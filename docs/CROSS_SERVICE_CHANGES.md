# Cross-Service Changes & Mechanical Guardrails (`atobar-flow`)

Query this file on demand with `rg -n "<keyword>" docs/CROSS_SERVICE_CHANGES.md` when touching `contracts/`, `flow-engine/`, `control-panel/`, or EC2 Spot `UserData`.

## 1. The 4-Place Wiring Checklist (`contracts` ↔ `flow-engine` ↔ `control-panel`)

When adding or changing a store method, table, or internal RPC across services:
1. **Contracts (`contracts/src/atobar_contracts/`):**
   - Add request/response Pydantic models in `api.py` (inheriting shared domain models where possible instead of duplicating field bounds).
   - Declare new persistence methods on the `Store` `Protocol` in `store.py`.
   - Regenerate JSON schemas if manifest/envelope models changed: `uv run --no-sync python -m atobar_contracts.schema --write`.
2. **Flow Engine (`flow-engine/src/flow_engine/`):**
   - Implement the method on both `SupabaseStore` and `MemoryStore` in `store.py` (every `MemoryStore` getter must return a `copy.deepcopy`, enforced by `test_ac6_memory_store_getters_return_deep_copies` in `tests/test_boundaries.py`).
   - Expose the `/internal/v1/...` or `/v1/runs/{run_id}/...` route in `api.py`.
3. **Control Panel (`control-panel/src/control_panel/`):**
   - Add the `FlowEngineClient` method and matching `_build_local_fallback_transport` route in `flow_engine_client.py` (keep response status codes and `"action"` payloads identical to `flow-engine/src/flow_engine/api.py`).
   - Add the `MemoryStore` implementation and read-only/stub method on `SupabaseStore` in `store.py`.
4. **Single-Writer Ownership (`tests/test_table_ownership.py`):**
   - Register any new Supabase table or PL/pgSQL RPC in `FE_OWNED_TABLES` / `FE_OWNED_RPCS` or `CP_OWNED_TABLES` in `tests/test_table_ownership.py`.

## 2. Spot Sandbox Environment Handover (`UserData` → `sudo --preserve-env`)

When passing a new environment variable to EC2 Spot sandbox runs:
1. Export the variable in `launch_spot_instance()` (`contracts/src/atobar_contracts/github_rest.py`).
2. Add the variable name to **both** `sudo --preserve-env=...` lines in `infra/terraform/control-panel/scripts/cloud_init.sh.tpl`.
3. Persist any per-run metadata on `public.run_credentials` via `store.create_run_credential(...)` keyword arguments — **never** mutate the dict returned by `store.get_run_credential(run_id)`.
4. **Mechanical Guard:** `test_ac7_spot_userdata_exports_survive_sudo` in `tests/test_boundaries.py` executes `launch_spot_instance` `UserData` with `bash -c '...; env -0'` and asserts every exported variable is listed in `--preserve-env=` on every `sudo` line of `cloud_init.sh.tpl` (unless documented in `EXEMPT_SPOT_EXPORTS`).

## 3. Store Protocol vs. `hasattr(store, ...)` Ratchet

- Do **not** add `hasattr(store, ...)` duck-typing in `contracts/src/`, `flow-engine/src/`, or `control-panel/src/`.
- Declare methods on `atobar_contracts.store.Store` and implement them on `SupabaseStore`, `MemoryStore`, and any test fake (`FakeStore`).
- Do **not** fabricate fallback state on `store` (e.g. fake issue lists) inside production GitHub adapters (`_FlowEngineGitHubAdapter`, `_LocalGitHubAdapter`).
- **Mechanical Guard:** `test_ac7_store_hasattr_ratchet` in `tests/test_boundaries.py` compares per-file `hasattr(*store*, ...)` AST counts against `tests/hasattr_store_baseline.json`. Counts may only go down; never increase `tests/hasattr_store_baseline.json`.

## 4. Concurrency, Idempotency & Atomic Counter Rules (`flow-engine`)

1. **Atomic Postgres Counter RPCs:** Any numeric counter updated across concurrent PRs or runs (e.g., `spent_usd`, `prs_merged`, `merges_without_progress`) must use an atomic PL/pgSQL RPC (`SET col = col + $2`), never Python read-modify-write.
2. **In-Flight Dispatch Claims with TTL:** Any Spot VM dispatch triggered by webhooks or `/internal/v1/metrics` must acquire an atomic conditional-update claim with a TTL (e.g. `store.claim_goal_planning(tenant_id, run_id, ttl_minutes=15)`) before calling `launch_spot_instance`, and clear the claim on halt or completion.
3. **Per-Issue Lock & Event Deduplication on `turn-complete`:** Every branch inside `handle_run_turn_complete` (`flow-engine/src/flow_engine/api.py`) must execute inside `with _lock_for_issue(tenant.id, int(issue_number)):` and deduplicate retries via `store.has_processed_event` / `store.record_processed_event`.
4. **Known exception — in-process mint limiter:** `MintRateLimiter` (`/github-token` 60s per-run limit) is process-local and holds only because FE runs one uvicorn worker / one replica; scaling out requires replacing it with an atomic DB claim.

## 5. Harness Entry-Point Shims & Discovery

- Canonical instruction files are root `AGENTS.md`, `AI.md`, and `.pi/skills/atobar-ops/SKILL.md`.
- Symlink shims (verified by `tests/test_harness_shims.py`):
  - `CLAUDE.md -> AGENTS.md`
  - `.cursorrules -> AI.md`
  - `.claude/skills/atobar-ops -> ../../.pi/skills/atobar-ops`
- **Devin CLI discovery (verified against Devin CLI `v3000.11.92` local docs `share/devin/docs/reference/configuration/read-config-from.mdx` and `share/devin/docs/extensibility/rules.mdx`):** Devin natively auto-loads root `AGENTS.md` (`read_config_from.agents_standard`) and imports `.claude/skills/**/SKILL.md` (`read_config_from.claude`), resolving `.claude/skills/atobar-ops` without an extra Devin-specific symlink.
