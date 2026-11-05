#!/bin/bash
# Control Panel bootstrap. Only a read-only AWS key and the admin token *digest*
# are templated in; every other secret is pulled from AWS by refresh.sh.
# Deploy logic lives in infra/control-panel/refresh.sh (pulled from git), so
# changing it does not require replacing this server.
set -euo pipefail
exec > >(tee -a /var/log/atobar-cp-init.log) 2>&1

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-buildx docker-compose-v2 git jq python3-jwt python3-cryptography python3-requests

install -d -m 700 /etc/atobar
cat > /etc/atobar/aws.env <<AWS
AWS_ACCESS_KEY_ID=${aws_access_key_id}
AWS_SECRET_ACCESS_KEY=${aws_secret_access_key}
AWS_DEFAULT_REGION=${aws_region}
AWS
cat > /etc/atobar/aws-kms.env <<AWSKMS
AWS_ACCESS_KEY_ID=${kms_aws_access_key_id}
AWS_SECRET_ACCESS_KEY=${kms_aws_secret_access_key}
AWS_DEFAULT_REGION=${aws_region}
AWS_REGION=${aws_region}
AWSKMS
echo -n "${admin_token_sha256}" > /etc/atobar/admin.sha256
echo -n "${factory_repo}" > /etc/atobar/factory_repo
echo -n "${git_ref}" > /etc/atobar/git_ref
echo -n "${cp_hostname}" > /etc/atobar/cp_hostname
echo -n "${ghcr_image_prefix}" > /etc/atobar/ghcr_image_prefix
chmod 600 /etc/atobar/*

cat > /etc/systemd/system/atobar-cp-refresh.service <<'UNIT'
[Unit]
Description=Atobar Control Panel refresh (pull GHCR images, restart on change)
After=docker.service network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/bin/bash -c 'if [ -x /opt/atobar-flow/infra/control-panel/refresh.sh ]; then exec /opt/atobar-flow/infra/control-panel/refresh.sh; else exec /usr/local/bin/atobar-cp-bootstrap; fi'
UNIT
cat > /etc/systemd/system/atobar-cp-refresh.timer <<'UNIT'
[Unit]
Description=Poll atobar-flow for Control Panel updates
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
UNIT

# First clone: refresh.sh is not on disk yet, so fetch it from the repo once.
cat > /usr/local/bin/atobar-cp-bootstrap <<'SCRIPT'
#!/bin/bash
set -euo pipefail
REPO=$(cat /etc/atobar/factory_repo)
REF=$(cat /etc/atobar/git_ref)
if ! git clone -q --branch "$REF" "https://github.com/$REPO.git" /opt/atobar-flow 2>/dev/null; then
  AWS="docker run --rm --env-file /etc/atobar/aws.env public.ecr.aws/aws-cli/aws-cli:2.17.0"
  ssm() { $AWS ssm get-parameter --name "$1" --with-decryption --query Parameter.Value --output text; }
  TOKEN=$(APP_ID="$(ssm /flow/github/app_id)" INST_ID="$(ssm /flow/github/app_installation_id)" APP_KEY="$(ssm /flow/github/app_private_key)" python3 - <<'PY'
import os, time, jwt, requests
now = int(time.time())
j = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": os.environ["APP_ID"]}, os.environ["APP_KEY"], algorithm="RS256")
r = requests.post(f"https://api.github.com/app/installations/{os.environ['INST_ID']}/access_tokens",
                  headers={"Authorization": f"Bearer {j}", "Accept": "application/vnd.github+json"}, timeout=15)
r.raise_for_status()
print(r.json()["token"])
PY
  )
  git clone -q --branch "$REF" "https://x-access-token:$TOKEN@github.com/$REPO.git" /opt/atobar-flow
fi
git -C /opt/atobar-flow remote set-url origin "https://github.com/$REPO.git"
exec /opt/atobar-flow/infra/control-panel/refresh.sh --force
SCRIPT
chmod 700 /usr/local/bin/atobar-cp-bootstrap

systemctl enable --now docker
systemctl daemon-reload
systemctl start atobar-cp-refresh.service
systemctl enable --now atobar-cp-refresh.timer
