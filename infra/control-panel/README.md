# Control Panel API (M0, #141)

Tenant-scoped API in front of Supabase. Source is in `control-panel/`; infrastructure is in `infra/terraform/control-panel-hetzner/` (HCP workspace `atobar-flow-control-panel-hetzner`, auto-apply).

- Hosted on Hetzner `cx23`, at `https://<cp_hostname>` (`cp_hostname` sensitive secret), with a free Let's Encrypt certificate from Caddy. A systemd timer runs `refresh.sh` every 5 minutes and pulls pre-built images from `ghcr.io/atobar-flow/control-panel` and `ghcr.io/atobar-flow/flow-engine` when `main` moves.
- Secrets are pulled from AWS at boot by a read-only IAM user (`flow-control-panel-hetzner`).
- The admin token's plaintext lives only in AWS SM `flow/control-panel/admin_token`.
- Redeploy after merging API changes: `ssh -i ~/.ssh/cp.key "root@$CP_HOSTNAME" atobar-cp-refresh`. SSH is break-glass only: set `ssh_allowed_cidrs`. Terraform (`hcloud_ssh_key.cp`) uploads the public key to Hetzner and stores the private key in AWS SM `flow/control-panel/ssh_private_key`; materialize it locally at `~/.ssh/cp.key` (`0600`) for `atobar-ops cp status`.

| Endpoint | Auth |
|---|---|
| `GET /healthz` | none |
| `GET /v1/tenant`, `GET /v1/tenant/wiki` | tenant token |
| `POST /v1/events` `{"kind":"issues.labeled","issue_number":N,"label":"flow:..."}` | tenant token; `target_repo` comes from the token, not the body |
| `GET /onboard/flow-trigger.yml?repo=owner/name` | none; the generated client trigger workflow, used by `atobar-init` |
| `POST/DELETE /v1/admin/tenants/{owner}/{repo}/tokens` | admin token |

## Browser UI (#149, #150)

`GET /` signs users in with GitHub (OAuth user flow of the `atobar-flow-agent` GitHub App). A user sees a tenant only if GitHub reports `pull` permission on its repo for their token. Logins in `CP_ADMIN_LOGINS` see every tenant. The session is an HMAC-signed `httpOnly`/`Secure`/`SameSite=Lax` cookie valid for 8h; the GitHub user token is never stored. Metrics are read-only; "Propose a metric change" opens a `flow:metrics` issue in the factory repo.

| Env | Source |
|---|---|
| `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET` | AWS SM `flow/control-panel/github_oauth` (`client_id`, `client_secret`) |
| `CP_SESSION_SECRET` | AWS SM `flow/control-panel/session_secret` (Terraform `random_password`) |
| `CP_ADMIN_LOGINS` | SSM `/flow/control-panel/admin_logins` (Terraform `cp_admin_logins`) |
| `CP_PUBLIC_URL` | `https://<cp_hostname>` |
| `CP_LLM_KEK` | AWS SM `flow/control-panel/llm_kek` (Terraform `random_id`, 32 bytes base64) |
| `CP_GITHUB_APP_SLUG` | optional, default `atobar-flow-agent` (for the "install the App" link) |

### Self-service tenants

A signed-in user who is a GitHub **admin** of a repo where the App is installed can:

- **Add it as a tenant**. It starts as `onboarding` in `shadow` mode with no metrics; `flow:onboard` (#147) fills them in. Until it is `active`, work labels are accepted but do not start agents.
- **Generate or rotate its API token** (`atb_…`). The token is shown once, with `gh secret set ATOBAR_TENANT_TOKEN -R owner/repo`; only its sha256 is stored, and rotating revokes the old one.
- **Bring their own LLM key**, starting with Gemini (#151). The key is validated with a free `models.list` call, then AES-256-GCM-encrypted under `CP_LLM_KEK` with `(tenant_id, provider)` as associated data, and stored in `tenant_llm_keys`. Supabase only sees ciphertext and the last 4 characters. The key is never returned to the browser.

All forms are POST with a per-session CSRF token and a same-origin check. Admin rights come from GitHub at sign-in and are cached in the 8-hour session.

`refresh.sh` re-renders `/etc/atobar/cp.env` every 5 minutes and restarts the stack when the commit or any of these values change. When they are unset, the UI shows "GitHub sign-in is not configured" and the API is unaffected. The App's callback URL must be `https://<cp_hostname>/auth/callback`.

### Onboarding guide

`GET /onboarding` is a public page with the admin's step-by-step guide, linked from the sign-in page, the home page ("Install on your repository") and every tenant page. It points to `GET /onboard.sh` ([`scripts/onboard.sh`](../../scripts/onboard.sh)). Maintainer details: [`docs/ONBOARDING.md`](../../docs/ONBOARDING.md).
