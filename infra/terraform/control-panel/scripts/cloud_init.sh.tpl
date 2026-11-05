#!/bin/bash
set -e

# Variables (Injected via Terraform templatefile)
WORKSPACE_DIR="/home/ubuntu/workspace"
ENGINE_DIR="/home/ubuntu/atobar-flow"
ENGINE_REF="$${ENGINE_REF:-main}"
TARGET_REPO="$${TARGET_REPO:?TARGET_REPO must be set}"
ISSUE_ID="$${ISSUE_ID:?ISSUE_ID must be set}"
export TARGET_REPO ISSUE_ID
AWS_REGION="${aws_region}"

# Instance identity for the central control plane (Axiom 10)
IMDS_TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" http://169.254.169.254/latest/meta-data/instance-id || hostname)
export INSTANCE_ID

# 1. Zero-Trust Token Acquisition via Control Panel (/v1/runs/{run_id}/github-token)
echo "Installing Zero-Trust git credential helper (/usr/local/bin/atobar-git-credential)..."
cat > /usr/local/bin/atobar-git-credential <<'CRED_HELPER'
#!/bin/bash
set -euo pipefail
if [ "$${1:-get}" != "get" ]; then
    exit 0
fi
CP_URL="$${CONTROL_PANEL_URL:?CONTROL_PANEL_URL is required}"
RUN_ID="$${FLOW_RUN_ID:?FLOW_RUN_ID is required}"
RUN_CREDENTIAL="$${FLOW_RUN_CREDENTIAL:?FLOW_RUN_CREDENTIAL is required}"
CACHE_DIR="/dev/shm/atobar-cred-$${RUN_ID}-$(id -u)"
CACHE_FILE="$${CACHE_DIR}/token.json"
mkdir -p -m 0700 "$CACHE_DIR"

mint_or_cached_token() {
    python3 - "$CP_URL" "$RUN_ID" "$RUN_CREDENTIAL" "$CACHE_FILE" <<'PY'
import datetime
import json
import os
import sys
import urllib.request

cp_url, run_id, run_cred, cache_file = sys.argv[1:5]
now = datetime.datetime.now(datetime.timezone.utc)

if os.path.isfile(cache_file):
    try:
        with open(cache_file, "r", encoding="utf-8") as fh:
            cached = json.load(fh)
        exp_raw = cached.get("expires_at", "")
        tok = cached.get("token", "")
        if tok and exp_raw:
            exp_dt = datetime.datetime.fromisoformat(exp_raw.replace("Z", "+00:00"))
            if now + datetime.timedelta(minutes=5) < exp_dt:
                print(tok)
                sys.exit(0)
    except Exception:
        pass

headers = {"Authorization": f"Bearer {run_cred}", "Accept": "application/json"}
inst_id = os.environ.get("INSTANCE_ID", "").strip()
if inst_id:
    headers["X-Instance-ID"] = inst_id
req = urllib.request.Request(
    f"{cp_url.rstrip('/')}/v1/runs/{run_id}/github-token",
    method="POST",
    headers=headers,
)
with urllib.request.urlopen(req, timeout=15) as resp:
    data = json.loads(resp.read().decode("utf-8"))

fd = os.open(cache_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(data, fh)

print(data["token"])
PY
}

TOKEN="$(mint_or_cached_token)"
echo "protocol=https"
echo "host=github.com"
echo "username=x-access-token"
echo "password=$${TOKEN}"
CRED_HELPER
chmod 0755 /usr/local/bin/atobar-git-credential
git config --system credential.helper /usr/local/bin/atobar-git-credential

export GITHUB_TOKEN="$(/usr/local/bin/atobar-git-credential get | sed -n 's/^password=//p')"
echo 'Defaults env_keep += "CONTROL_PANEL_URL FLOW_RUN_ID FLOW_RUN_CREDENTIAL GITHUB_TOKEN INSTANCE_ID"' > /etc/sudoers.d/99-atobar-env
chmod 0440 /etc/sudoers.d/99-atobar-env
UBUNTU_UID="$(id -u ubuntu)"
UBUNTU_CACHE_DIR="/dev/shm/atobar-cred-$${FLOW_RUN_ID}-$${UBUNTU_UID}"
mkdir -p -m 0700 "$UBUNTU_CACHE_DIR"
python3 - "/dev/shm/atobar-cred-$${FLOW_RUN_ID}-0/token.json" "$${UBUNTU_CACHE_DIR}/token.json" <<'PY'
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as fh:
    d = json.load(fh)
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump({"token": d.get("token", ""), "expires_at": d.get("expires_at", "")}, fh)
PY
chown -R ubuntu:ubuntu "$UBUNTU_CACHE_DIR"
chmod 0600 "$${UBUNTU_CACHE_DIR}/token.json"
sudo -u ubuntu git config --global user.name "flow-agent"
sudo -u ubuntu git config --global user.email "agent@atobar-flow.local"

# Issue-less runs (metric_compiler, admin onboarding) have ISSUE_ID=0: there is no issue to fetch.
if [ "$ISSUE_ID" != "0" ]; then
    echo "Fetching Issue $ISSUE_ID from GitHub..."
    ISSUE_JSON=$(curl -s -f -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github.v3+json" "https://api.github.com/repos/$TARGET_REPO/issues/$ISSUE_ID") || {
        echo "ERROR: Failed to fetch issue $ISSUE_ID from $TARGET_REPO" >&2
        exit 1
    }
    ISSUE_TITLE=$(echo "$ISSUE_JSON" | jq -r '.title')
    ISSUE_BODY=$(echo "$ISSUE_JSON" | jq -r '.body')
    export ISSUE_TEXT="Title: $ISSUE_TITLE

$ISSUE_BODY"
else
    export ISSUE_TEXT=""
fi

# 2. Phase 0 Scaffolding (Axiom 5)
echo "Cloning repositories..."
sudo -u ubuntu git clone "https://github.com/$TARGET_REPO.git" $WORKSPACE_DIR

echo "Cloning Engine Repository..."
if ! [[ "$ENGINE_REF" =~ ^[0-9a-f]{40}$ ]]; then
    echo "ERROR: ENGINE_REF ($ENGINE_REF) must be a full 40-character commit SHA to guarantee supply-chain integrity." >&2
    exit 1
fi

FACTORY_REPO="${factory_repo}"
if [ -z "$FACTORY_REPO" ] || [[ "$FACTORY_REPO" == *"{factory_repo}"* ]]; then
    FACTORY_REPO="atobar-flow/atobar-flow"
fi
ENGINE_TOKEN="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("engine_token") or d.get("token",""))' "/dev/shm/atobar-cred-$${FLOW_RUN_ID}-0/token.json")"
rm -rf "/dev/shm/atobar-cred-$${FLOW_RUN_ID}-0"
if ! git -c credential.helper= clone "https://github.com/$${FACTORY_REPO}.git" "$ENGINE_DIR" 2>/dev/null; then
    git -c credential.helper= clone "https://x-access-token:$${ENGINE_TOKEN}@github.com/$${FACTORY_REPO}.git" "$ENGINE_DIR"
fi
git -C "$ENGINE_DIR" remote set-url origin "https://github.com/$${FACTORY_REPO}.git"
unset ENGINE_TOKEN
chown -R ubuntu:ubuntu "$ENGINE_DIR"
sudo -u ubuntu bash -c "cd $ENGINE_DIR && git checkout $ENGINE_REF"

echo "Executing deterministic Phase 0 setup for Client Workspace..."
cd $WORKSPACE_DIR
sudo -u ubuntu mkdir -p ./.atobar
BASE_SHA=$(sudo -u ubuntu git rev-parse HEAD 2>/dev/null || echo "base")
if [ -f "./.atobar/setup.sh" ]; then
    sudo -u ubuntu chmod +x ./.atobar/setup.sh
    sudo -u ubuntu ./.atobar/setup.sh > /home/ubuntu/phase0_setup.log 2>&1
    echo "$BASE_SHA" | sudo -u ubuntu tee "./.atobar/.setup_complete_$${BASE_SHA}" "./.atobar/.setup_complete_base" > /dev/null
fi
sudo -u ubuntu mkdir -p /home/ubuntu/worktrees

echo "Setting up Agent Engine..."
cd $ENGINE_DIR
if ! command -v uv &> /dev/null && ! sudo -u ubuntu bash -lc "command -v uv" &> /dev/null; then
    sudo -u ubuntu curl -LsSf https://astral.sh/uv/0.12.11/install.sh | sudo -u ubuntu HOME=/home/ubuntu bash
fi
if [ -d "$ENGINE_DIR/flow-agent" ]; then
    sudo -u ubuntu bash -lc "export PATH=\"/usr/local/bin:/home/ubuntu/.local/bin:\$PATH\" && cd $ENGINE_DIR && uv sync --locked --package flow-agent" > /home/ubuntu/engine_setup.log 2>&1
elif [ "$FLOW_AGENT_RUNTIME" = "nanobot" ] || grep -q '"agent_runtime": *"nanobot"' "$WORKSPACE_DIR/.atobar/manifest.json" 2>/dev/null || grep -q '"agent_runtime": *"nanobot"' "$ENGINE_DIR/tenants/"*.json 2>/dev/null; then
    sudo -u ubuntu bash -lc "export PATH=\"/usr/local/bin:/home/ubuntu/.local/bin:\$PATH\" && cd $ENGINE_DIR && uv sync --locked --extra agents" > /home/ubuntu/engine_setup.log 2>&1
else
    sudo -u ubuntu bash -lc "export PATH=\"/usr/local/bin:/home/ubuntu/.local/bin:\$PATH\" && cd $ENGINE_DIR && uv sync --locked" > /home/ubuntu/engine_setup.log 2>&1
fi

# 3. Agent Handoff
echo "Phase 0 Complete. Environment is ready for Agent Loop."

echo "Starting Agent Loop (agent_runtime=$${FLOW_AGENT_RUNTIME:-nanobot})..."
export DISPLAY=":0"
if [ -d "/home/ubuntu/atobar-flow/flow-agent" ]; then
    sudo --preserve-env=DISPLAY,ISSUE_TEXT,GITHUB_TOKEN,INSTANCE_ID,TARGET_REPO,ISSUE_ID,FLOW_ROLE,FLOW_TENANT,FLOW_PR,FLOW_HEAD_SHA,TARGET_REF,CONTROL_PANEL_URL,FLOW_RUN_ID,FLOW_RUN_CREDENTIAL,FLOW_AGENT_RUNTIME,LABEL_NAME,ENGINE_REF,FLOW_METRIC_KEY,FLOW_METRIC_INTENT_B64,FLOW_GOAL_MODE,FLOW_PLAN_COUNT,FLOW_GOAL_OBJECTIVE_B64 -u ubuntu bash -lc 'cd /home/ubuntu/workspace && \
        uv run --no-sync --directory /home/ubuntu/atobar-flow --package flow-agent python -m flow_agent.main' 2>&1 | tee -a /home/ubuntu/agent.log
else
    sudo --preserve-env=DISPLAY,ISSUE_TEXT,GITHUB_TOKEN,INSTANCE_ID,TARGET_REPO,ISSUE_ID,FLOW_ROLE,FLOW_TENANT,FLOW_PR,FLOW_HEAD_SHA,TARGET_REF,CONTROL_PANEL_URL,FLOW_RUN_ID,FLOW_RUN_CREDENTIAL,FLOW_AGENT_RUNTIME,LABEL_NAME,ENGINE_REF,FLOW_METRIC_KEY,FLOW_METRIC_INTENT_B64,FLOW_GOAL_MODE,FLOW_PLAN_COUNT,FLOW_GOAL_OBJECTIVE_B64 -u ubuntu bash -lc 'cd /home/ubuntu/workspace && \
        uv run --no-sync --directory /home/ubuntu/atobar-flow python -m flow_engine.main' 2>&1 | tee -a /home/ubuntu/agent.log
fi
