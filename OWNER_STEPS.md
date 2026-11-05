
# J01
- Configure `SUPABASE_URL` in `.env` and sync via `uv run --no-sync atobar-ops infra sync-vars --write`.

# J02
- Fill `/flow/github/{app_id,app_installation_id,app_private_key}` in AWS SSM Parameter Store via `atobar-ops init-app` and `atobar-ops infra up --write`.

# J07
- Paste the pilot's Gemini key into `https://$CP_HOSTNAME/tenants/arsenyspb/ollama-monitor` as a BYO-key.

# J10
1. Install the `atobar-flow-agent` GitHub App on `arsenyspb/ollama-monitor` (or confirm via repo Settings -> GitHub Apps).
2. Sign in at `https://$CP_HOSTNAME` -> Add `arsenyspb/ollama-monitor` -> Generate tenant token (`atb_…`, shown once).
3. In a local clone of `ollama-monitor`: `curl -fsSL "https://$CP_HOSTNAME/onboard.sh" | bash` — sets `ATOBAR_TENANT_TOKEN` secret and opens the trigger PR -> **merge it** (replaces the legacy PAT trigger with the generated CP-authenticated one).
4. Fill the spec issue the script opens (`## Target state`, metrics, arbitration) -> apply `flow:onboard`.
5. Check `/v1/events` -> `flow_onboard` -> `onboard-tenant.yml` runs -> renders manifest PR on atobar-flow + opens `.atobar/` + `flow-metrics.yml` PR on the pilot -> review + merge both.

### J11: Roadmap and Hygiene
1. Merge PR #229
2. No infrastructure redeployment needed.

# J13
- Delete the legacy `/flow/apps/*` parameters from AWS SSM Parameter Store (9 keys in total).
