# J36 — Cloud Security Posture Management (CSPM) Assessment & Remediation: Artifact Confidentiality, KMS Envelope BYOK LLM Keys, Sandbox Zero-Trust & Control Plane Hardening

- **Priority:** P0 (Immediate Security & Confidentiality Blocker)
- **Status:** Open / Active Priority
- **Milestone:** M06 (Security, Isolation & Multi-Tenant Hardening)
- **Owner Directives:**
  1. Conduct an all-encompassing Cloud Security Posture Management (CSPM) assessment across the entire `atobar-flow` architecture (Control Panel on Hetzner, AWS EC2 Spot sandboxes, IAM/SSM/Secrets Manager/KMS, Supabase data plane, and GitHub integration).
  2. Eliminate public unauthenticated exposure of visual artifacts (`https://$CP_HOSTNAME/v1/runs/<run_id>/screenshots/<filename>.png`) so proprietary tenant UI/TUI/code data can never leak.
  3. Design and implement a defence-in-depth cryptographic strategy for storing, rotating, auditing, and delivering user-submitted BYOK LLM API keys.

---

## Part 1 — Executive CSPM Assessment (Architecture-Wide Findings)

| ID | Severity | Layer | Finding & Exact Code Location | Risk / Blast Radius |
| :--- | :---: | :--- | :--- | :--- |
| **CSPM-01** | **CRITICAL** | Artifact Confidentiality (`$CP_HOSTNAME`) | `GET /v1/runs/{run_id}/screenshots/{filename}` (`src/control_panel/app.py:424-460`) requires **no authentication** and emits `Cache-Control: public, max-age=31536000`. Raw PNGs are cached unencrypted in world-traversable `/tmp/atobar-screenshots` and stored unencrypted as `data_base64` in `public.run_events`. | Anyone with a screenshot URL (or shared proxy/CDN cache) can view proprietary terminal/UI screenshots from private tenant repositories without authorization; unencrypted artifacts persist indefinitely in `/tmp` and `run_events`. |
| **CSPM-02** | **CRITICAL** | Spot Sandbox IAM & Cross-Tenant Isolation (`AWS IAM / SSM`) | `flow-spot-sandbox-role` (`infra/terraform/identity/iam_instance.tf:19-28`) grants every Spot instance `ssm:GetParameter` on `/flow/github/app_private_key`, and `src/flow_engine/conductor/dispatch.py` writes `https://oauth2:${GITHUB_TOKEN}@github.com` to `/home/ubuntu/.git-credentials` under the same `ubuntu` UID that runs untrusted repo tests and `nanobot`'s `execute_command`. | A malicious or prompt-injected test/command inside a tenant sandbox can read the **Factory GitHub App Private Key** from AWS SSM (compromising **all** onboarded tenants) or exfiltrate `GITHUB_TOKEN` and `FLOW_RUN_CREDENTIAL` from the `ubuntu` user environment. |
| **CSPM-03** | **HIGH** | BYOK LLM Key Cryptography & Delivery (`Control Panel / AWS KMS`) | `src/control_panel/llm_keys.py` encrypts all tenant LLM keys under a single static symmetric key (`CP_LLM_KEK`) generated via `random_id.llm_kek` in Terraform (`infra/terraform/control-panel-hetzner/main.tf:35-49`) with no key rotation path (`KEK_VERSION = 1`). Furthermore, `POST /v1/runs/{run_id}/llm-key` (`src/control_panel/app.py:210-285`) allows repeated plaintext fetches until `expires_at` and fans out **all** configured provider keys (`keys: dict[str, str]`) instead of only the single key needed for the run's role. | Compromise of `/etc/atobar-cp/env` or Terraform state exposes both `CP_LLM_KEK` and `SUPABASE_SERVICE_KEY`, allowing offline decryption of every tenant's LLM keys with zero CloudTrail audit trail. Over-broad multi-provider fan-out increases secret exposure in Spot sandboxes. |
| **CSPM-04** | **HIGH** | Terraform State Secret Exposure (`Hetzner / AWS Secrets Manager`) | `infra/terraform/control-panel-hetzner/main.tf` generates `random_password.admin_token`, `random_password.session_secret`, `random_id.llm_kek`, `tls_private_key.ssh`, and `aws_iam_access_key.cp` inside Terraform resources. Additionally, untracked local `terraform.tfstate` files exist under `infra/terraform/foundation/`. | Root secrets (`admin_token`, `session_secret`, `llm_kek`, break-glass SSH private key, and Hetzner IAM access key) are persisted in plaintext inside Terraform state. |
| **CSPM-05** | **HIGH** | Run Credential Expiration & Replay (`Control Panel API`) | While `POST /v1/runs/{run_id}/llm-key` checks `record["expires_at"]`, `POST /v1/runs/{run_id}/heartbeat` (`src/control_panel/heartbeat.py:30-50`), `GET /v1/runs/{run_id}/arbitration-data`, `POST /v1/runs/{run_id}/arbitration-decisions`, `POST /v1/runs/{run_id}/events`, and `POST /v1/runs/{run_id}/screenshots` (`src/control_panel/app.py:289-421`) **never verify `expires_at`** and do not bind `target_repo` from the authenticated `tenant_id`. | Leaked or expired `FLOW_RUN_CREDENTIAL` tokens remain valid indefinitely for injecting fake heartbeats, events, screenshots, and arbitration decisions, and can spoof `target_repo` in `sessions`. |
| **CSPM-06** | **MEDIUM** | Control Panel Web CSRF & Rate Limiting (`src/control_panel/web.py`) | State-mutating browser `POST` routes (`/tenants/{owner}/{repo}/llm-keys`, `/llm-keys/{provider}/delete`, `/token`, `/arbitration-mode`, `/llm-roles/{role}`) rely solely on `SameSite=Lax` cookies without an anti-CSRF token, and API auth endpoints lack rate limiting. | Cross-site request forgery if a subdomain or top-level navigation gadget is abused; brute-force / DoS risk on token and webhook endpoints. |
| **CSPM-07** | **MEDIUM** | Supabase Data Plane RLS & Audit Logging (`infra/supabase/migrations/`) | Several tables have `ENABLE ROW LEVEL SECURITY` without explicit `service_role` policies or lack an append-only `security_audit_log` tracking secret access (`tenant_llm_keys` create/rotate/delete/decrypt events). | Inconsistent RLS posture across migrations and no centralized audit trail of tenant secret lifecycle events. |

---

## Part 2 — Target Security Architecture & Strategy

### 1. Private-by-Default Visual Artifact & Screenshot Architecture (Fixes CSPM-01)
1. **Eliminate Unauthenticated Public Screenshot Access**:
   - Remove unauthenticated public access from `GET /v1/runs/{run_id}/screenshots/{filename}`.
   - Enforce **Dual-Mode Authorized Access**:
     - **Browser / Control Panel Users**: Require a valid `cp_session` cookie (`require_user`) AND verify repository access via `user_can_access(user, tenant.owner, tenant.repo)` (or verify the user is a collaborator on the repository).
     - **GitHub Issue / PR Markdown Rendering (Signed Capability URLs)**: When embedding screenshot URLs in GitHub comments/PRs, generate a short-lived, cryptographically signed capability URL (`https://$CP_HOSTNAME/v1/runs/{run_id}/screenshots/{filename}?exp=<ts>&sig=<hmac_sha256>`) using a dedicated `CP_ARTIFACT_SIGNING_KEY` bound to `(tenant_id, run_id, filename, exp)`.
   - **Strict Response Headers**: Always emit `Cache-Control: private, no-store, max-age=0`, `X-Content-Type-Options: nosniff`, and `Content-Security-Policy: default-src 'none'; img-src 'self'`.
2. **Encrypted-at-Rest Storage & TTL Garbage Collection**:
   - Never store plaintext `data_base64` in `public.run_events`. Encrypt screenshot bytes with AES-256-GCM bound to `(tenant_id, run_id, filename)` before persisting on disk (`/var/lib/atobar-cp/screenshots`, mode `0700`) or in the database.
   - Add automatic TTL cleanup (default 7 days, configurable per tenant) in `flow_gc` and Control Panel startup/background sweep.

### 2. KMS Envelope Encryption Strategy for User-Submitted BYOK LLM Keys (Fixes CSPM-03)
```
┌────────────────────────────────────────────────────────────────────────────┐
│                     AWS KMS Customer Managed Key (CMK)                     │
│        alias/flow-tenant-llm-cmk (Automatic Annual Rotation + CloudTrail)  │
└───────────────────┬────────────────────────────────────┬───────────────────┘
                    │ KMS:GenerateDataKey                │ KMS:Decrypt
                    │ Context: {tenant_id, provider}     │ Context: {tenant_id, provider}
                    ▼                                    ▼
┌───────────────────────────────────────┐  ┌─────────────────────────────────┐
│          Key Submission (Write)       │  │     Run-Time Delivery (Read)    │
│ 1. Validate key against provider API  │  │ 1. Verify unexpired single-use  │
│ 2. Request fresh 256-bit DEK from KMS │  │    FLOW_RUN_CREDENTIAL + role   │
│ 3. Encrypt API key with AES-256-GCM   │  │ 2. Call KMS:Decrypt(wrapped_dek,│
│    under plaintext DEK + AAD          │  │    Context={tenant_id,provider})│
│ 4. Zeroize plaintext DEK in memory    │  │ 3. Decrypt only the single      │
│ 5. Store (wrapped_dek, ciphertext,    │  │    provider key for that role   │
│    kek_version, kms_key_arn) in DB    │  │ 4. Mark credential consumed &   │
│ 6. Record event in security_audit_log │  │    append to security_audit_log │
└───────────────────────────────────────┘  └─────────────────────────────────┘
```
1. **Per-Key Envelope Encryption (`wrapped_dek` + `ciphertext`)**:
   - Provision `aws_kms_key.tenant_llm_cmk` (`enable_key_rotation = true`) in `infra/terraform/identity/`.
   - Add `wrapped_dek text`, `encryption_mode text default 'kms_envelope'`, and `kms_key_id text` columns to `public.tenant_llm_keys`.
   - When a user submits an LLM key, the Control Panel calls `kms:GenerateDataKey` with `EncryptionContext={"tenant_id": str(tenant_id), "provider": provider, "purpose": "tenant_llm_key"}`, encrypts the API key with AES-256-GCM using the returned 32-byte DEK, immediately zeroizes the plaintext DEK, and stores `(wrapped_dek, ciphertext)` in `public.tenant_llm_keys`.
   - Even if both the Hetzner host disk (`/etc/atobar-cp/env`) and the entire Supabase database (`public.tenant_llm_keys`) are dumped, **zero tenant LLM keys can be decrypted offline**, and every decryption call is logged in **AWS CloudTrail** with the exact `tenant_id` and `provider` in the KMS `EncryptionContext`!
2. **Seamless Legacy Migration & Key Re-Wrapping (`MultiVersion` Support)**:
   - Support transparent migration from `kek_version = 1` (local `CP_LLM_KEK`) to `kek_version = 2` (`kms_envelope`) via a migration command that re-wraps existing rows into KMS envelope keys without requiring tenants to re-enter their keys.
3. **Least-Privilege Single-Role Delivery & Atomic Credential Consumption**:
   - In `POST /v1/runs/{run_id}/llm-key`, deliver **only** the API key for the single provider configured for that run's `role` (remove multi-provider fan-out unless a role explicitly requires a secondary provider).
   - Bind `run_credentials` consumption to the first `instance_id` that claims the run so a credential cannot be replayed from an external IP/host.

### 3. Zero-Trust Spot Sandbox & Secret Isolation (Fixes CSPM-02, CSPM-04)
1. **Remove Factory GitHub App Private Key from Spot Instance IAM Role**:
   - Remove `aws_ssm_parameter.github_app_private_key.arn` from `flow-spot-sandbox-role` in `infra/terraform/identity/iam_instance.tf`.
   - Instead, mint the short-lived, repository-scoped GitHub installation token in the Control Panel (`POST /v1/runs/{run_id}/github-token`, authenticated via `FLOW_RUN_CREDENTIAL`) or in the Conductor before dispatch! The Spot instance never touches the Factory GitHub App Private Key.
2. **Privilege Separation & Ephemeral Credential Handling on Spot**:
   - Never write `https://oauth2:${GITHUB_TOKEN}@github.com` to a persistent `/home/ubuntu/.git-credentials` file readable by untrusted subprocesses; use an in-memory `GIT_ASKPASS` helper owned by `root`/`flow-engine` or scrub credentials before invoking untrusted test suites.
   - Enforce IMDSv2 (`http_tokens = "required"`, `http_put_response_hop_limit = 1`) and block raw IMDS access (`169.254.169.254`) from unprivileged subprocesses.

### 4. Control Plane API, CSRF & Database Hardening (Fixes CSPM-05, CSPM-06, CSPM-07)
1. **Enforce `expires_at` & Tenant Binding on Every `/v1/runs/{run_id}/*` Endpoint**:
   - Centralize run credential validation into a single FastAPI dependency `require_valid_run_credential(run_id, authorization, store)` that enforces:
     - Constant-time `hmac.compare_digest(record["cred_hash"], cred_hash)`
     - Strict `datetime.now(timezone.utc) <= expires_at` check (`410 Gone` on expired credentials)
     - Authoritative `tenant_id` and `target_repo` resolution from `store.tenant_by_id(record["tenant_id"])` (never trusting `payload.target_repo` from the client body).
2. **CSRF Tokens on All Control Panel Web POST Forms**:
   - Derive a per-session HMAC CSRF token (`csrf_token = hmac.new(secret, session_cookie, sha256)`) and require it on all state-mutating `POST` routes in `src/control_panel/web.py`.
3. **Append-Only `security_audit_log` & Explicit RLS Policies in Supabase**:
   - Create `public.security_audit_log` (append-only via `flow_reject_mutation` trigger) recording all sensitive actions (`llm_key.upserted`, `llm_key.deleted`, `llm_key.delivered`, `tenant_token.issued`, `tenant_token.revoked`, `screenshot.accessed`) with `tenant_id`, `actor`, `run_id`, `ip_hash`, and `created_at`.

---

## Part 3 — Work Packages & TDD Execution Plan

- [ ] **WP-1 (CSPM-01 — Private Screenshot Architecture & Signed Capability URLs)**:
  - Lock down `GET /v1/runs/{run_id}/screenshots/{filename}` behind session auth (`user_can_access`) OR short-lived HMAC capability signature (`?exp=...&sig=...`).
  - Replace `Cache-Control: public, max-age=31536000` with `Cache-Control: private, no-store, max-age=0` and `X-Content-Type-Options: nosniff`.
  - Encrypt cached/stored screenshot payloads with AES-256-GCM bound to `(tenant_id, run_id, filename)` and move disk cache from `/tmp/atobar-screenshots` to `0700` private storage with TTL purge.
- [ ] **WP-2 (CSPM-03 — KMS Envelope Encryption & Least-Privilege Delivery for BYOK LLM Keys)**:
  - Add `EnvelopeKeyManager` in `src/control_panel/llm_keys.py` supporting AWS KMS `GenerateDataKey` / `Decrypt` (`kek_version = 2`) with `EncryptionContext={"tenant_id": tenant_id, "provider": provider}` alongside seamless fallback/re-wrap for `kek_version = 1`.
  - Restrict `POST /v1/runs/{run_id}/llm-key` to return only the single provider key required for the run's assigned `role` and bind `run_credentials` to the claiming `instance_id`.
  - Add Terraform KMS CMK (`alias/flow-tenant-llm-cmk`) with automatic rotation enabled and migration `0018_cspm_security_hardening.sql` (`wrapped_dek`, `kms_key_id`, and `security_audit_log`).
- [ ] **WP-3 (CSPM-05 & CSPM-06 — Unified Run Credential Expiry Enforcement & Web CSRF Protection)**:
  - Extract `require_run_credential` dependency in `src/control_panel/app.py` and `src/control_panel/heartbeat.py` enforcing `hmac.compare_digest`, `expires_at` (`410 Gone`), and authoritative `target_repo` binding across `/heartbeat`, `/events`, `/screenshots`, `/arbitration-data`, and `/arbitration-decisions`.
  - Add per-session CSRF token generation and validation across all `POST` forms in `src/control_panel/web.py`.
- [ ] **WP-4 (CSPM-02 & CSPM-04 — Spot Sandbox Secret Zero-Trust & GitHub Token Minting via Control Plane)**:
  - Add `POST /v1/runs/{run_id}/github-token` on the Control Panel so the Control Panel mints the repository-scoped installation token for the run's `tenant.repo` and the Spot instance no longer needs `ssm:GetParameter` on `/flow/github/app_private_key`.
  - Remove `aws_ssm_parameter.github_app_private_key.arn` from `flow-spot-sandbox-role` in `infra/terraform/identity/iam_instance.tf` and remove local untracked `.tfstate` files / harden `.gitignore`.
- [ ] **WP-5 (CSPM-07 — Supabase RLS Audit & Append-Only Security Audit Log)**:
  - Add `0018_cspm_security_hardening.sql` with `public.security_audit_log` (append-only trigger), explicit `service_role_all` RLS policies on all tables, and `wrapped_dek` columns on `public.tenant_llm_keys`.
