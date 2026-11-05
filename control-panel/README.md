# control-panel

## Purpose
Public FastAPI gateway and browser UI (`CP_PUBLIC_URL=https://$CP_HOSTNAME`) handling GitHub OAuth sign-in, tenant onboarding, KMS envelope BYOK LLM key storage, signed screenshot serving, and webhook/agent run endpoints.

## Runtime
Stateless FastAPI/Uvicorn container (`control-panel/Dockerfile`, port `8080`) behind Caddy TLS on Hetzner Cloud.

## APIs
- Browser UI: `GET /`, `GET /onboarding`, `GET /login`, `GET /auth/callback`, `POST /logout`, `/tenants/*`
- Tenant & CI API: `GET /v1/tenant`, `GET /v1/tenant/wiki`, `POST /v1/events`, `POST /v1/metrics`
- Run API (`Depends(require_run_credential)`): `POST /v1/runs/{id}/{heartbeat,llm-key,github-token,turn-complete,events,screenshots,arbitration-decisions}`, `GET /v1/runs/{id}/{arbitration-data,wiki,screenshots/{filename}}`
- Admin & Health: `GET /healthz`, `GET /v1/admin/status`, `POST|DELETE /v1/admin/tenants/{owner}/{repo}/tokens`

## Consumes
- `atobar_contracts` for shared schemas, envelopes, state machine, and provider validators
- Supabase PostgREST for persistence
- AWS KMS (`GenerateDataKey`, `Decrypt`) for envelope encryption of tenant BYOK LLM keys
- GitHub App API for installation tokens and repository dispatch

## Data
Reads/writes `tenants`, `tenant_tokens`, `tenant_llm_keys`, `tenant_role_llm`, `metric_definitions`, `metric_observations`, `run_credentials`, `sessions`, `run_events`, `arbitration_decisions`, and `security_audit_log`.

## Credentials
Configured via `/etc/atobar/cp.env`: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY`, `CP_ADMIN_TOKEN_SHA256`, `CP_SESSION_SECRET`, `CP_LLM_KEK`, `CP_KMS_KEY_ID`, `CP_ARTIFACT_SIGNING_KEY`.

## Allowed imports
Stdlib, `atobar_contracts.*`, `control_panel.*`, `fastapi`, `pydantic`, `httpx`, `supabase`, `boto3`, `cryptography`, `jwt`. Forbidden: `flow_engine`, `flow_agent`, `atobar_ops`, `nanobot`, `mcp`.

## Develop & test
```bash
uv run --no-sync pytest control-panel/tests -q
uv run --no-sync mypy control-panel/src --ignore-missing-imports
docker build -t control-panel:local -f control-panel/Dockerfile .
```

## Troubleshooting
- **410 Gone on `/v1/runs/{id}/*`**: Run credential `expires_at` has passed; check Spot dispatch latency or clock skew.
- **403 Forbidden on browser POST**: Session CSRF token mismatch or expired cookie; refresh the page and sign in again.

## Future repo
`github.com/atobar-flow/control-panel`
