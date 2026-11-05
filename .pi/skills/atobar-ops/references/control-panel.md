# Control Panel (Hetzner) Reference

Terraform: `infra/terraform/control-panel-hetzner/`. Live at `https://$CP_HOSTNAME` (composed from the `CP_HOSTNAME` secret).

## Layout & Break-Glass SSH Key (`~/.ssh/cp.key`)

- **Terraform provisioning (`infra/terraform/control-panel-hetzner/main.tf`)**: `tls_private_key.ssh` generates an `ED25519` keypair, registers the public key on Hetzner (`hcloud_ssh_key.cp`), and stores the private key in AWS Secrets Manager at `flow/control-panel/ssh_private_key` (port 22 is gated by `SSH_ALLOWED_CIDRS`).
- **Local workstation pre-flight**: `atobar-ops cp status` assumes `~/.ssh/cp.key` (`0600`) holds that Terraform-provisioned key:
  ```bash
  (umask 077 && aws secretsmanager get-secret-value --secret-id flow/control-panel/ssh_private_key --region "${AWS_REGION:-us-west-2}" --query SecretString --output text > ~/.ssh/cp.key)
  ```
- Host reached via `ssh -i ~/.ssh/cp.key "root@$CP_HOSTNAME"`.
- Checkout lives at `/opt/atobar-flow` on the host; `atobar-ops cp status` reads its
  `git rev-parse HEAD` and compares it to `origin/main`.
- `atobar-cp-refresh.timer` runs `atobar-cp-refresh.service` on a schedule: it pulls `main`, and
  only rebuilds the Docker containers if the checkout actually changed (watch the journal for
  "Deactivated successfully" vs. a rebuild log).
- The app runs as `docker compose` services (commonly `control-panel-api-1`,
  `control-panel-caddy-1` — Caddy fronts TLS/routing, the API container is FastAPI in `control-panel/src/control_panel/app.py`).

## Routes worth probing

- `/` and `/login` should be reachable (200 / redirect to GitHub OAuth).
- `/tenants/<owner>/<repo>` and `/tenants/<owner>/<repo>/llm-keys` require a session — an
  unauthenticated request should redirect to `/login`, never return tenant data directly.
- `/v1/runs/{run_id}/screenshots/{file}` is evaluator/developer screenshot storage — it must not be
  publicly readable. `atobar-ops cp probe` flags a 200 on a nonexistent run/file as suspicious
  (it should 404 or require auth).

## LLM keys

BYOK keys are AES-256-GCM-encrypted under a KEK pulled from AWS Secrets Manager
(`flow/control-panel/llm_kek`), envelope-bound to `(tenant_id, provider)` — see
`control-panel/src/control_panel/llm_keys.py`. `atobar-ops cp keycheck <provider>` re-runs the same provider
validation call the Control Panel makes, using a key from the `KEY` env var (never an argument).
Nothing it checks is ever persisted.

## Drift

There's no automated Terraform-vs-host diff; `cp status`'s docker/journal output is a best-effort
starting point — compare container names and images against
`infra/terraform/control-panel-hetzner/main.tf` by hand for anything that looks off.
