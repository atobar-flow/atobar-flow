# J43 (cloud-infra-and-data-plane-hardening): Path 2 — Infrastructure & Cloud Security Best Practices: Least-Privilege GitHub OIDC Role, No Cross-Trust Warm VM Reuse, Fail-Closed Secret Refresh & KMS Activation

> **⛔ Blocked by:** nothing. **Start here.**
> **✅ Unblocks:** **J44** (#339) needs **WP-2** merged · **J42 WP-5** (#337) needs **WP-3** merged · **WP-5's migration** takes the number after **J42 WP-1**'s migration.
>
> **Global order (same in J42/J43/J44):** ① J43 WP-1 → ② J42 WP-1 + J43 WP-2 (same milestone, pair them) → ③ J42 WP-2, J42 WP-3, J42 WP-4, J43 WP-3, J43 WP-4 (parallel) → ④ J42 WP-5 (after J43 WP-3) → ⑤ J44 WP-2…WP-5 (after J42 WP-1, J42 WP-2, J43 WP-2) → ⑥ J43 WP-5, J42 WP-6 (any time after ②). J44 WP-1 is independent and may start at any step.

- **Priority:** P0 (WP-1, WP-2) / P1 (WP-3, WP-4) / P2 (WP-5)
- **Milestone:** M06 (Security, Isolation & Multi-Tenant Hardening)
- **UX Impact:** **None for users.** The one trade-off is operational: evaluator and arbitrator turns always start on a fresh Spot VM, so they start a few minutes slower and cost a little more.
- **Review status:** Security second review of the as-implemented code done on 2026-10-22. Findings were re-ranked, the WP-2 fix was replaced, and INF-04's env split was deferred. See "Review log" at the bottom.

---

## FINAL VERDICT AND TODO:

**Verdict:** Both CRITICALs are real, but two of the original fixes would not have worked as written. (a) **INF-01:** pinning `sub` is right, but the bigger win is deleting leftovers. Only `supabase-migrate.yml` still uses `flow-gha-dispatcher-role`; its EC2/PassRole permissions and the whole `iam_onboarding.tf` policy belong to workflows that no longer exist. (b) **INF-02:** making `$ENGINE_DIR` root-owned does **not** stop a developer turn from tampering with the next evaluator turn. The agent user `ubuntu` is in the `docker` group (`install_deps.sh:42`, root-equivalent) and almost certainly has the Ubuntu cloud image's passwordless `sudo`. The simple, sufficient fix is: **never hand a warm VM over *into* the `evaluator` or `arbitrator` role.** (c) **INF-04's env split** would break the web UI, because `web.py` uses the GitHub App key inside the `api` container. Defer it; ship fail-closed refresh now.

**TODO (in order):**
1. [x] **WP-1 — Shrink and pin the GitHub OIDC role (INF-01):** pin `sub` to `main`. Delete the leftover EC2/PassRole/onboarding permissions; keep only the Supabase credential read the migration workflow needs.
2. [x] **WP-2 — No warm handover into verifying roles (INF-02):** add a single guard in `dispatch` so `claim_warm` is ignored when `act.role in {"evaluator", "arbitrator"}`.
3. [x] **WP-3 — Fail-closed `refresh.sh` (INF-04 partial, J42 UJ-05):** abort before writing `cp.env` when a **required** secret is empty. Keep optional secrets optional.
4. [x] **WP-4 — Turn on KMS envelope encryption (INF-03):** grant `kms:GenerateDataKey`/`kms:Decrypt` on the CMK and export `CP_KMS_KEY_ID`. Existing keys keep decrypting via `CP_LLM_KEK`.
5. [x] **WP-5 — Low-cost hygiene (INF-05, INF-06):** default EBS encryption for the account, bind VNC websockify to localhost, pin the `uv` installer version, and a migration adding `search_path` to the `SECURITY DEFINER` functions (audit-table triggers only with an offboarding escape hatch).
6. [ ] **Housekeeping (manual owner action — GHCR storage cap):** Check & delete the leftover `control-panel` container package in GHCR (`https://github.com/users/arsenyspb/packages`, or run `gh auth refresh -s read:packages,delete:packages` and delete `control-panel` if present). Verified in code (`rg -n "ghcr\.io" .`) that no workflow or runtime script references `ghcr.io` since `control_panel_build.yml` stopped pushing on Oct 19, 2026 (`refresh.sh` builds images locally on the Hetzner host).
7. [ ] Deferred (separate issue, not zero-complexity): splitting `cp.env` between `api` and `flow-engine` (INF-04 part 1), and removing `sudo`/`docker` from the agent user (INF-02 defense in depth).

---

## Part 1 — Ranked Infrastructure & Cloud Security Findings ("As-Implemented", verified)

| ID | Severity | Cloud Layer & Exact Code Location | Verified Claim | Blast Radius |
| :--- | :---: | :--- | :--- | :--- |
| **INF-01** | **CRITICAL** | **AWS IAM GitHub OIDC trust**<br>`infra/terraform/foundation/iam_oidc.tf:7-27, 29-110`<br>`infra/terraform/foundation/iam_onboarding.tf:1-34`<br>`.github/workflows/supabase-migrate.yml:17-44` | 1. The trust policy accepts `sub = "repo:atobar-flow/atobar-flow:*"`: any branch, any `workflow_dispatch` ref, any job in the factory repo that sets `id-token: write`.<br>2. The role can read `flow/supabase/credentials-*`, `flow/control-panel/admin_token-*`, SSM `/flow/github/app_*` (the **factory GitHub App private key**) and `/flow/gemini/api_key`, write `flow/tenants/*` secrets, and launch Spot instances with `iam:PassRole`.<br>3. **Only `supabase-migrate.yml` assumes this role today.** `iam_onboarding.tf:1` says it is for `onboard-tenant.yml`, and the EC2 statements served `flow_dispatcher.yml`; neither workflow exists any more (`ls .github/workflows`). Dispatch moved to Flow Engine.<br>4. Correction: the missing `aud` condition is **not** exploitable. AWS already checks the audience against the OIDC provider's `client_id_list = ["sts.amazonaws.com"]` (`iam_oidc.tf:3`). Adding it is defense in depth only. | Anyone who can push a branch to `atobar-flow/atobar-flow` (a collaborator, or a compromised automation token with `contents`+`workflows` write) can take the Supabase service key and DB password, the CP admin token and the factory App private key. That is a full multi-tenant compromise. |
| **INF-02** | **CRITICAL** (with `auto_merge`) / **HIGH** otherwise | **Warm Spot VM reuse across trust levels**<br>`flow-agent/src/flow_agent/main.py:692-713`<br>`contracts/src/atobar_contracts/conductor_io.py:631-652`<br>`infra/terraform/control-panel/scripts/cloud_init.sh.tpl:104, 108-114, 165-168`<br>`infra/packer/scripts/install_deps.sh:42` | 1. After a developer turn, the **same Python process and VM** keep running as the evaluator (or arbitrator, which merges under `auto_merge`). `dispatch` accepts `claim_warm` for **any** role.<br>2. Everything runs as `ubuntu`. Developer turns run untrusted repo code (tests, `.atobar/setup.sh`, `execute_command`) as that user.<br>3. `ubuntu` is in the `docker` group (root-equivalent), and the official Ubuntu cloud image grants it passwordless `sudo` (`/etc/sudoers.d/90-cloud-init-users`). The Packer build doesn't remove that; **verify on the current AMI**.<br>4. `chown -R ubuntu:ubuntu "$ENGINE_DIR"` makes the engine code writable, but because of (3) making it root-owned would not help. | Code planted during a developer turn persists into the evaluator/arbitrator turn on the same VM. It can read the next role's `FLOW_RUN_CREDENTIAL` and GitHub token, alter the engine code, and post an approving verdict, or merge. Together with J42 UJ-09 this is self-approval. |
| **INF-03** | **MEDIUM** (rated lower) | **KMS envelope encryption not wired up**<br>`infra/control-panel/refresh.sh:58-75`<br>`infra/terraform/control-panel-hetzner/main.tf` (no `kms:*`)<br>`control-panel/src/control_panel/llm_keys.py` | Confirmed: `CP_KMS_KEY_ID` is never exported and the CP IAM user has no KMS permissions, so every BYOK key is sealed with the local `CP_LLM_KEK` (`kek_version=1`). Rated lower: the same host holds the AWS credentials, so KMS adds **audit (CloudTrail), rate limiting and revocation**, not prevention against a root compromise of the host. | No audit trail for LLM key decrypts, and anyone who reads both `cp.env` and the `tenant_llm_keys` table can decrypt keys offline. |
| **INF-04** | **MEDIUM** (fail-open refresh) / **LOW** (shared env, deferred) | **Control Panel host secrets**<br>`infra/control-panel/refresh.sh:43-51, 85`<br>`infra/control-panel/docker-compose.yml:1-55`<br>`control-panel/src/control_panel/web.py:627, 1616` (`get_dispatcher` in `api`) | 1. **Fail-open (confirmed, narrower than first claimed):** `SB` and `APP_KEY` already abort under `set -euo pipefail`. `SESSION`, `LLM_KEK` and `ADMINS` use `|| true`, and an empty `SESSION`/`LLM_KEK` is written to `cp.env` and the containers are restarted. `OAUTH` is **optional by design** (`|| echo '{}'`).<br>2. **Shared env (confirmed, fix deferred):** `api`, `flow-engine` and `janitor` all mount `cp.env` (App private key and Supabase service key). AWS credentials (`aws.env`) are already **not** mounted into `api`. Splitting is not a config-only change: `api` uses the App key today (`web.py` → `GitHubAppDispatcher`), and the Supabase service key can only be split by adding a scoped DB role.<br>3. The `sslip.io` fallback (`refresh.sh:43`) is a legitimate first-boot path. It relies on a third-party DNS service but is not a hijack vector on its own. Keep it, and log a warning. | (1) A transient AWS failure can blank the session/artifact signing key (→ J42 UJ-05 defaults) or the KEK (→ every run fails to get its LLM key). (2) An RCE in the internet-facing `api` exposes the App key and service key. |
| **INF-05** | **LOW** (rated lower) | **Spot sandbox AMI**<br>`infra/packer/files/vncproxy.service:8`<br>`infra/terraform/foundation/security.tf:1-44`<br>`infra/terraform/control-panel/launch_template.tf:34-37`<br>`infra/packer/scripts/install_deps.sh` | 1. websockify listens on `0.0.0.0:6080` to an unauthenticated VNC. However, the Spot security group has **no ingress rules** ("Zero-inbound", `security.tf:3`), so it is not reachable over the network. Local processes already run as the same user. → defense in depth only.<br>2. No EBS encryption on the template or the AMI (confirmed).<br>3. `uv` installed with `curl … | sh` without a version pin or checksum (confirmed).<br>Positive: IMDSv2 is required with hop limit 1, and the instance role explicitly **denies** `ssm:GetParameter*` and `secretsmanager:*` (`identity/iam_instance.tf:16-31`). | Tenant source code sits unencrypted on EBS snapshots/volumes. Supply-chain drift in `uv`. |
| **INF-06** | **LOW** (rated lower) | **Supabase functions & audit tables**<br>`infra/supabase/migrations/0020_service_cutover.sql:50, 82, 108`<br>`infra/supabase/migrations/0015_run_events.sql` | 1. Three `SECURITY DEFINER` functions have no `SET search_path` (`0022_tenant_goals.sql` already does it right). Exploiting it needs a role with `CREATE` on a schema earlier in the search path; only `service_role` touches the DB → low.<br>2. `run_events` and `*_history` have no `flow_reject_mutation()` trigger. No code updates or deletes them today. | Hardening only. |

---

## Part 2 — Work Packages

- [x] **WP-1 (Fixes INF-01 — least-privilege, main-only OIDC role) — P0, Terraform only**
  - `iam_oidc.tf` trust policy: `StringEquals { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com", "token.actions.githubusercontent.com:sub" = "repo:atobar-flow/atobar-flow:ref:refs/heads/main" }`. Use `StringEquals`, not `StringLike`: no wildcards.
  - Rename the role's purpose to "supabase migrations" in the description, and **remove** every statement that `supabase-migrate.yml` doesn't use: the EC2 `Describe*`/`RunInstances`/`CreateTags`/`TerminateInstances` statements, `iam:PassRole`, and the whole `aws_iam_role_policy.gha_onboarding` (`iam_onboarding.tf`). Keep only `secretsmanager:GetSecretValue` on `flow/supabase/credentials-*`. Before deleting, confirm with `rg -n "flow-gha-dispatcher-role|gha_dispatcher" .` that nothing else uses it (as of this review: only `supabase-migrate.yml` and `foundation/outputs.tf`). Keep the role name so the workflow's `ROLE_ARN` doesn't change.
  - Update `flow-engine/README.md:24`, which still says Flow Engine uses this role.
- [x] **WP-2 (Fixes INF-02 — no warm handover into verifying roles) — P0**
  - `contracts/src/atobar_contracts/conductor_io.py` `dispatch` (`:631`): change `if claim_warm and claimed_dispatch is None:` to also require `act.role not in WARM_HANDOVER_FORBIDDEN_ROLES`, where `WARM_HANDOVER_FORBIDDEN_ROLES = frozenset({"evaluator", "arbitrator"})` is defined next to it. Those actions fall through to the normal cold Spot launch path, which already exists.
  - Leave the allowed handovers unchanged (`planner → developer`, `evaluator → developer`): the next role is equally or less trusted than what already ran on the VM.
  - `flow-agent/src/flow_agent/main.py:692-713` needs no change: when the reply isn't `continue`, it already shuts down cleanly.
  - Add a unit test in the existing conductor test module: a developer `AckEvent` processed with `claim_warm=True` returns no `handover`, and launches an evaluator.
  - Docs: one line in `docs/AGENTS.md` (Axiom 11, role isolation): "Verifying roles never inherit a VM that ran untrusted developer code."
- [x] **WP-3 (Fixes INF-04 fail-open + J42 UJ-05 infra side) — P1**
  - `infra/control-panel/refresh.sh`: after fetching, `for v in SESSION LLM_KEK APP_KEY; do [ -n "${!v}" ] || { echo "FATAL: $v empty; keeping previous cp.env" >&2; exit 1; }; done`, placed **before** `ENV_NEW` is written (`:85`). Also require `${#SESSION} -ge 32`.
  - Keep `OAUTH` and `ADMINS` optional (fresh installs without GitHub sign-in must still deploy).
  - Keep the `sslip.io` fallback, but print `WARN: using sslip.io hostname` so operators notice.
- [x] **WP-4 (Fixes INF-03 — activate KMS) — P1**
  - `infra/terraform/control-panel-hetzner/main.tf` `aws_iam_user_policy.cp`: allow `kms:GenerateDataKey` and `kms:Decrypt` on the `alias/flow-tenant-llm-cmk` key ARN (look up the key by alias with `data "aws_kms_alias"`; avoid `key/*`), with condition `kms:EncryptionContextKeys` ⊇ {`tenant_id`, `provider`}.
  - `refresh.sh`: `echo "CP_KMS_KEY_ID=alias/flow-tenant-llm-cmk"` in `ENV_NEW`.
  - The container that calls KMS (`api`, for `/v1/runs/{run_id}/llm-key`) needs AWS credentials. Today `aws.env` is only mounted into `flow-engine`/`janitor`. Either mount a **separate** `aws-kms.env` with a KMS-only IAM user into `api`, or route decrypts through Flow Engine. Pick the KMS-only user: it's simpler, and `api` still gets no SM/SSM access.
  - No re-encryption job is needed: `kek_version=1` rows keep decrypting via `CP_LLM_KEK`, and new or rotated keys get `kek_version=2`.
- [ ] **WP-5 (Fixes INF-05 & INF-06 — hygiene) — P2**
  - `aws_ebs_encryption_by_default { enabled = true }` in `foundation` (one resource covers the template, the AMI builds and future volumes). Existing AMIs need a rebuild; old snapshots stay unencrypted until they're pruned.
  - `vncproxy.service`: `websockify 127.0.0.1:6080 localhost:5901`. First confirm `inspect_ui`/screenshot paths connect via localhost; they use CDP `localhost:29229`, not VNC.
  - Pin `uv`: `https://astral.sh/uv/<exact-version>/install.sh` in `install_deps.sh` and `cloud_init.sh.tpl` (same version as CI).
  - New migration (use the **next free number** at implementation time; J42 also adds one): `ALTER FUNCTION … SET search_path = public` for `record_metric_definitions_history`, `record_tenants_manifest_history`, `approve_metric`, `flow_reject_mutation`. Add `flow_reject_mutation()` `BEFORE UPDATE OR DELETE` triggers on `metric_definitions_history` and `tenants_manifest_history`. For `run_events`, make it `BEFORE UPDATE` only: deletes must stay possible for tenant offboarding and retention, via `ON DELETE CASCADE` from `run_credentials` (`0015_run_events.sql:4`).

---

## Part 3 — Edge Cases Audit

| Detail | Concurrency / Race | Partial Failure / Retry Idempotency | Pagination / Bounds / Truncation | Auth / Trust Boundary |
| :--- | :--- | :--- | :--- | :--- |
| **OIDC role pin to `main`** | The workflow uses `concurrency: supabase-migrate`, which isn't affected. | `workflow_dispatch` with `dry_run` from a feature branch now fails to assume the role. That's intended: dry-runs go through `main`. Say so in the workflow's input description. | — | Apply the Terraform first, **then** delete the policies. Run `terraform plan` to confirm only the dispatcher role/policies change. |
| **No warm handover into evaluator/arbitrator** | Two dispatch actions in one step (e.g. evaluator + label updates): only the first eligible one can be warm-claimed. The guard runs per action, so later non-forbidden actions still can't claim once `claimed_dispatch` is set (unchanged). | If the cold launch fails (Spot capacity), the existing launch retry/escalation path applies. The previous warm VM must **not** be kept as a fallback for the evaluator. | Adds one cold start (~2-4 min) per review round. Cost is visible in `run_events` (`warm_reuse: false`). | With J42 WP-1, an evaluator credential only ever exists on a VM that never ran developer code. |
| **`refresh.sh` fail-closed** | `refresh.sh` runs as a systemd oneshot, so there's no overlap. | Abort leaves the old `cp.env` and running containers untouched. The next timer tick retries. | `SESSION` must be ≥32 chars, otherwise abort. | A blank signing key can no longer reach production. |
| **First boot (no secrets yet)** | — | A fresh host with no `session_secret`/`llm_kek` in SM now fails to deploy. Both are created by Terraform in `infra/terraform/control-panel-hetzner/main.tf` (`random_password` / `random_id`, see `infra/control-panel/README.md:25-28`), so the normal path is unaffected. Apply that workspace before the first refresh. | — | Fail loud rather than run with defaults. |
| **KMS activation** | Stateless per-key calls. | KMS outage: new key saves fail with a clear error, but `kek_version=1` keys keep working. `kek_version=2` decrypts fail → run fails with "LLM key unavailable" (same as today when the KEK is missing). | — | Every decrypt is in CloudTrail with `tenant_id`/`provider` context. |
| **`run_events` immutability** | — | `UPDATE` is blocked; `DELETE` via the offboarding cascade still works. | — | Audit rows can't be silently rewritten by a leaked service key that lacks owner rights. |

---

## Deferred (file as a separate issue; not zero-complexity)

- **Split `cp.env`** so the internet-facing `api` holds neither the GitHub App private key nor the Supabase service key. Needs: moving `web.py`'s `GitHubAppDispatcher` calls behind Flow Engine's internal API, and a scoped Postgres role for CP-owned tables.
- **Remove `docker` group membership and passwordless `sudo` from the agent user** after Phase 0. Tenant `.atobar/setup.sh` scripts commonly need `sudo apt-get`, so revoke **after** Phase 0 setup, never before. Developers that `sudo` during a turn would break, so this needs its own announcement. WP-2 makes this defense in depth rather than a blocker.

---

## Review log (2026-10-22 security second review)

- **INF-01:** kept CRITICAL. Added "delete the leftover EC2/PassRole/onboarding permissions" (only `supabase-migrate.yml` uses the role). Corrected the `aud` claim (AWS already enforces it via `client_id_list`).
- **INF-02:** kept CRITICAL. **Replaced** the root-owned `$ENGINE_DIR` fix (ineffective while `ubuntu` has `docker` + `sudo`) with a one-line guard: no warm handover into `evaluator`/`arbitrator`. Moved `sudo`/`docker` removal to Deferred.
- **INF-03:** HIGH → MEDIUM (audit and revocation, not prevention). Added the missing piece: `api` has no AWS credentials today.
- **INF-04:** split into the fail-open refresh (MEDIUM, WP-3) and the env split (LOW, deferred: the original WP-3 would have broken `web.py`). Kept the `sslip.io` fallback (first-boot path).
- **INF-05:** MEDIUM → LOW (the security group allows no inbound traffic). Replaced per-template EBS flags with account-level default encryption.
- **INF-06:** MEDIUM → LOW. `run_events` now blocks UPDATE only, so offboarding deletes still work.
- Migration numbers left as "next free" to avoid a collision with J42.
