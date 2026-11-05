#!/bin/bash
# Pull secrets, update the checkout to the configured ref, pull pre-built GHCR
# images (or build locally only when CP_BUILD_FROM_SOURCE=1), and (re)start the stack.
# Run at boot by cloud-init and every 5 minutes by atobar-cp-refresh.timer.
set -euo pipefail

AWS="docker run --rm --env-file /etc/atobar/aws.env public.ecr.aws/aws-cli/aws-cli:2.17.0"
ssm() { $AWS ssm get-parameter --name "$1" --with-decryption --query Parameter.Value --output text; }
REPO=$(cat /etc/atobar/factory_repo)
REF=$(cat /etc/atobar/git_ref)
GHCR_PREFIX=$(cat /etc/atobar/ghcr_image_prefix 2>/dev/null || echo "ghcr.io/${REPO%%/*}")
DIR=/opt/atobar-flow

APP_ID=$(ssm /flow/github/app_id)
INST_ID=$(ssm /flow/github/app_installation_id)
APP_KEY=$(ssm /flow/github/app_private_key)

mint_token() {
  if [ -z "$APP_ID" ] || [ "$APP_ID" = "UNCONFIGURED" ] || [ -z "$INST_ID" ] || [ "$INST_ID" = "UNCONFIGURED" ] || [ -z "$APP_KEY" ] || [ "$APP_KEY" = "UNCONFIGURED" ]; then
    return 1
  fi
  APP_ID="$APP_ID" INST_ID="$INST_ID" APP_KEY="$APP_KEY" python3 - <<'PY'
import os, time, jwt, requests
now = int(time.time())
j = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": os.environ["APP_ID"]}, os.environ["APP_KEY"], algorithm="RS256")
r = requests.post(f"https://api.github.com/app/installations/{os.environ['INST_ID']}/access_tokens",
                  headers={"Authorization": f"Bearer {j}", "Accept": "application/vnd.github+json"}, timeout=15)
r.raise_for_status()
print(r.json()["token"])
PY
}

BEFORE=$(git -C "$DIR" rev-parse HEAD 2>/dev/null || echo none)
if [ -d "$DIR/.git" ]; then
  if ! git -C "$DIR" fetch -q "https://github.com/$REPO.git" "$REF" 2>/dev/null; then
    TOKEN=$(mint_token)
    git -C "$DIR" fetch -q "https://x-access-token:$TOKEN@github.com/$REPO.git" "$REF"
  fi
  git -C "$DIR" checkout -q -f FETCH_HEAD
else
  if ! git clone -q --branch "$REF" "https://github.com/$REPO.git" "$DIR" 2>/dev/null; then
    TOKEN=$(mint_token)
    git clone -q --branch "$REF" "https://x-access-token:$TOKEN@github.com/$REPO.git" "$DIR"
  fi
  git -C "$DIR" remote set-url origin "https://github.com/$REPO.git"
fi
AFTER=$(git -C "$DIR" rev-parse HEAD)

if [ "$BEFORE" != "$AFTER" ] && [ "${ALREADY_REEXEC:-}" != "yes" ]; then
  export ALREADY_REEXEC=yes
  exec "$DIR/infra/control-panel/refresh.sh" "$@"
fi

HOST=$(cat /etc/atobar/cp_hostname 2>/dev/null || true)
if [ -z "$HOST" ]; then
  HOST="$(curl -s http://169.254.169.254/hetzner/v1/metadata/public-ipv4).sslip.io"
  echo "WARN: using sslip.io hostname ($HOST)" >&2
fi

# EC-6: Verify whether cp_hostname resolves to this server's public IPv4 and restart Caddy on transition.
PUBLIC_IPV4="$(curl -fsS --max-time 5 http://169.254.169.254/hetzner/v1/metadata/public-ipv4 2>/dev/null || true)"
RESOLVED_IPV4="$(getent ahostsv4 "$HOST" 2>/dev/null | awk '{print $1; exit}' || true)"
DNS_CHANGED=no
if [ -n "$PUBLIC_IPV4" ] && [ "$RESOLVED_IPV4" = "$PUBLIC_IPV4" ]; then
  PREV_DNS="$(cat /etc/atobar/dns_ready 2>/dev/null || true)"
  if [ "$PREV_DNS" != "$PUBLIC_IPV4" ]; then
    echo "$PUBLIC_IPV4" > /etc/atobar/dns_ready
    DNS_CHANGED=yes
  fi
fi

SB=$($AWS secretsmanager get-secret-value --secret-id flow/supabase/credentials --query SecretString --output text)
OAUTH=$($AWS secretsmanager get-secret-value --secret-id flow/control-panel/github_oauth --query SecretString --output text 2>/dev/null || echo '{}')
SESSION=$($AWS secretsmanager get-secret-value --secret-id flow/control-panel/session_secret --query SecretString --output text 2>/dev/null || true)
ADMINS=$(ssm /flow/control-panel/admin_logins 2>/dev/null || true)
LLM_KEK=$($AWS secretsmanager get-secret-value --secret-id flow/control-panel/llm_kek --query SecretString --output text 2>/dev/null || true)
FE_TOKEN=$($AWS secretsmanager get-secret-value --secret-id flow/control-panel/fe_internal_token --query SecretString --output text 2>/dev/null || true)
if [ -z "$FE_TOKEN" ]; then
  if [ ! -s /etc/atobar/fe_internal_token ]; then
    (umask 077; python3 -c "import secrets; print(secrets.token_hex(32))" > /etc/atobar/fe_internal_token)
  fi
  FE_TOKEN=$(cat /etc/atobar/fe_internal_token)
fi

for v in SESSION LLM_KEK APP_KEY; do
  [ -n "${!v}" ] || { echo "FATAL: $v empty; keeping previous cp.env" >&2; exit 1; }
done
[ "${#SESSION}" -ge 32 ] || { echo "FATAL: SESSION must be >= 32 chars; keeping previous cp.env" >&2; exit 1; }

KMS_AWS_ENV=$($AWS secretsmanager get-secret-value --secret-id flow/control-panel/kms_aws_env --query SecretString --output text 2>/dev/null || true)
if [ -n "$KMS_AWS_ENV" ]; then
  (umask 077; printf '%s\n' "$KMS_AWS_ENV" > /etc/atobar/aws-kms.env)
fi

ENV_NEW=$(
  echo "SUPABASE_URL=$(jq -r .api_url <<<"$SB")"
  echo "SUPABASE_SERVICE_KEY=$(jq -r .service_key <<<"$SB")"
  echo "GITHUB_APP_ID=$APP_ID"
  echo "GITHUB_APP_INSTALLATION_ID=$INST_ID"
  echo "GITHUB_APP_PRIVATE_KEY=\"$(awk '{printf "%s\\n", $0}' <<<"$APP_KEY")\""
  echo "CP_ADMIN_TOKEN_SHA256=$(cat /etc/atobar/admin.sha256)"
  echo "FLOW_FACTORY_REPO=$REPO"
  echo "GITHUB_OAUTH_CLIENT_ID=$(jq -r '.client_id // ""' <<<"$OAUTH")"
  echo "GITHUB_OAUTH_CLIENT_SECRET=$(jq -r '.client_secret // ""' <<<"$OAUTH")"
  echo "CP_SESSION_SECRET=$SESSION"
  echo "CP_ADMIN_LOGINS=$ADMINS"
  echo "CP_PUBLIC_URL=https://$HOST"
  echo "CP_LLM_KEK=$LLM_KEK"
  echo "CP_KMS_KEY_ID=alias/flow-tenant-llm-cmk"
  echo "FE_URL=http://flow-engine:8081"
  echo "FE_INTERNAL_TOKEN=$FE_TOKEN"
  echo "ENGINE_REF=$AFTER"
)

ENV_SAME=no
[ -f /etc/atobar/cp.env ] && [ "$ENV_NEW" = "$(cat /etc/atobar/cp.env)" ] && ENV_SAME=yes
if [ "$BEFORE" = "$AFTER" ] && [ "$ENV_SAME" = yes ] && [ "$DNS_CHANGED" = no ] && [ "${ALREADY_REEXEC:-}" != "yes" ] && [ "${1:-}" != "--force" ] && docker compose -f "$DIR/infra/control-panel/docker-compose.yml" ps --status running -q api | grep -q . && docker compose -f "$DIR/infra/control-panel/docker-compose.yml" ps --status running -q flow-engine | grep -q . && docker compose -f "$DIR/infra/control-panel/docker-compose.yml" ps --status running -q janitor | grep -q .; then
  exit 0
fi

(umask 077; printf '%s\n' "$ENV_NEW" > /etc/atobar/cp.env)

IMAGE_TAG="${IMAGE_TAG:-sha-$AFTER}"
export CP_IMAGE="${GHCR_PREFIX}/control-panel:${IMAGE_TAG}"
export FE_IMAGE="${GHCR_PREFIX}/flow-engine:${IMAGE_TAG}"

cd "$DIR/infra/control-panel"
if [ "${CP_BUILD_FROM_SOURCE:-0}" = "1" ]; then
  docker build -t "$CP_IMAGE" -f "$DIR/control-panel/Dockerfile" "$DIR"
  docker build -t "$FE_IMAGE" -f "$DIR/flow-engine/Dockerfile" "$DIR"
else
  if ! docker compose pull api flow-engine janitor 2>/dev/null; then
    export CP_IMAGE="${GHCR_PREFIX}/control-panel:latest"
    export FE_IMAGE="${GHCR_PREFIX}/flow-engine:latest"
    if ! docker compose pull api flow-engine janitor 2>/dev/null; then
      echo "WARN: GHCR pull failed (package may still be private); falling back to local build" >&2
      docker build -t "$CP_IMAGE" -f "$DIR/control-panel/Dockerfile" "$DIR"
      docker build -t "$FE_IMAGE" -f "$DIR/flow-engine/Dockerfile" "$DIR"
    fi
  fi
fi

CP_HOSTNAME="$HOST" docker compose up -d --remove-orphans api flow-engine janitor caddy
if [ "$DNS_CHANGED" = yes ]; then
  docker compose restart caddy
fi
docker compose restart api flow-engine janitor
docker image prune -f >/dev/null

cat > /etc/atobar/deploy.json <<EOF
{
  "status": "ok",
  "commit": "$AFTER",
  "deployed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
