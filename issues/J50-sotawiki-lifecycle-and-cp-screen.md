# J50 (sotawiki-lifecycle-and-cp-screen): SotaWiki Lifecycle: Mermaid Architecture Synthesis, Coalesced Auto-Refresh Queue, Control Panel Repo Screen & Dead-Code Purge

- **Priority:** P1
- **Milestone:** M07 (Autonomous Loop & Context Quality)
- **Cross-Service Scope:** `contracts` ↔ `flow-engine` ↔ `control-panel` ↔ `flow-agent` + 1 Supabase migration

---

## 1. Simplification, Speed & Complexity-Reduction Thesis

1. **No new role and no new Spot `UserData` env vars (`issue_id == 0` convention):**
   - `metric_compiler` already uses `issue_id = 0` (`flow-engine/src/flow_engine/api.py:2663`) for Control-Panel-dispatched Spot runs that are not bound to a GitHub issue.
   - Re-use the existing `onboarder` role for wiki regeneration with `issue_id = 0`. In `run_onboarder()` (`flow-agent/src/flow_agent/roles/onboarder/runner.py:89`), only run `render_into()` + `publish_onboarding_pr()` when `issue_id > 0` (initial `flow:onboard` issue). When `issue_id == 0` (CP "Force Regenerate" or auto-refresh after $N$ merges), `run_onboarder()` only synthesizes and upserts `SotaWiki` at default-branch `HEAD`.
   - **Result:** Zero changes to `launch_spot_instance()`, `cloud_init.sh.tpl` (`sudo --preserve-env`), `ROLE_ALLOWLISTS`, or `control-panel/auth.py` (`POST /v1/runs/{run_id}/wiki` stays `frozenset({"onboarder"})`).
2. **No new queue table or background daemon (1-bit coalescing queue on `repository_wikis`):**
   - Whether 1 or 15 commits land on the default branch while an `onboarder` refresh run is already active (`store.active_session_for_role(tenant.id, "onboarder")`), at most **one** follow-up run against latest `HEAD` is ever needed.
   - Store `refresh_status` (`idle | queued | running | failed`) and `refresh_queued: bool` directly on `public.repository_wikis`. When an active `onboarder` run finishes in `handle_run_turn_complete`, if `refresh_queued` is `true`, dispatch one follow-up refresh and clear the flag.
3. **One canonical `SotaWiki` schema across all 4 packages:**
   - Today `SotaWiki` uses `key_technologies`, `top_level_structure`, `key_concepts`, while `repository_wikis` uses `technologies`, `structure`, `concepts`, forcing every store (`flow-engine/store.py:679-681`, `control-panel/store.py:826-828`) to carry `or wiki_row.get("key_...")` shims.
   - Move `SotaWiki` to `atobar_contracts` with canonical column names (`purpose`, `technologies`, `structure`, `concepts`, `architecture_mermaid`, `commit_sha`) + `AliasChoices` for LLM JSON tolerance.
4. **Faster, single-pass `_gather_context()` (`flow-agent/src/flow_agent/knowledge/onboard_repo.py`):**
   - Replace the 3-level nested subprocess fallback (`rg` → `find` → `ls`) and dead remote `git clone` helper with a single deterministic pass over the local checkout: `git rev-parse HEAD`, `rg --files --max-depth 3`, `README.md`, `AGENTS.md`/`CLAUDE.md`, `docs/` index, and top-level dependency manifests.

---

## 2. Dead-Code & Bug Purge Table

| Target | Location | Why It Is Dead / Broken | Action |
| :--- | :--- | :--- | :--- |
| `wiki_parser.py` (`parse_wikis`) | `flow-agent/src/flow_agent/knowledge/wiki_parser.py`<br>`flow-agent/tests/knowledge/test_wiki_parser.py` | Scans local `./.atobar/wikis/*.md` into `<knowledge-hints>`. Zero production callers since `#115`. | **Delete** module and test file; remove from `flow-agent/src/flow_agent/knowledge/README.md`. |
| `generate_wiki` tool in `FLOW_TOOLS` | `flow-agent/src/flow_agent/runtime/tools.py:6,28,38`<br>`flow-agent/tests/runtime/test_tools.py:22,35,48` | Registered in `FLOW_TOOLS` and excluded by every role allowlist (`J44 AS-14`). | **Remove** import and entry from `FLOW_TOOLS` and `_DEVELOPER_TOOLS` exclusion list. |
| `generate_wiki()`, `_resolve_repo_path()`, `_llm_from_env()`, `SotaWiki.to_markdown()`, `__main__` CLI | `flow-agent/src/flow_agent/knowledge/onboard_repo.py:25-34,69-88,123-131,192-230`<br>`flow-agent/tests/knowledge/test_wiki.py` | Only `synthesize_wiki`, `upsert_wiki_to_control_plane`, and `_gather_context` have production callers (`onboarder/runner.py`, `metric_compiler/runner.py`). `step_wiki` in `tenancy/onboard.py` was already removed. | **Delete** dead functions/CLI and fold `test_wiki.py` assertions into `test_onboard_repo.py`. |
| Redundant `fetch_run_wiki` in `planner` | `flow-agent/src/flow_agent/roles/planner/runner.py:185-186` | `main.py:549` already fetches `wiki` at Phase 0 boot and passes it to `run_planner()`. | **Remove** duplicate network fetch fallback. |
| Python `repr(list)` formatting bug in `format_wiki_section` | `flow-agent/src/flow_agent/roles/base.py:42-47` | `f"- Technologies: {wiki['technologies']}"` interpolates `list[str]` as `['Python', 'FastAPI']` instead of markdown bullets. | **Fix** to render clean markdown lists + fenced `architecture_mermaid` block inside `wrap_untrusted(..., source="tenant_wiki")`. |

---

## 3. Work Packages

### WP-1: Mermaid-First `SotaWiki` Contract, Prompt Upgrade & Dead-Code Purge (`contracts`, `flow-agent`)
- **Files:**
  - `contracts/src/atobar_contracts/wiki.py` (or `api.py`), `flow-agent/src/flow_agent/knowledge/onboard_repo.py`, `flow-agent/src/flow_agent/roles/base.py`, `flow-agent/src/flow_agent/runtime/tools.py`
  - Delete `flow-agent/src/flow_agent/knowledge/wiki_parser.py` & `flow-agent/tests/knowledge/test_wiki_parser.py`
- **Changes:**
  - Define canonical `SotaWiki` model with `purpose: str`, `technologies: list[str]`, `structure: str`, `concepts: list[str]`, `architecture_mermaid: str = ""`, `commit_sha: str = ""`.
  - Expand `_gather_context(repo_path)` to return `(head_sha, readme, agents_md, tree, deps)` in one pass (`AGENTS.md`/`CLAUDE.md` up to 5 KB, `README.md` up to 10 KB, `rg --files --max-depth 3`, dependency files up to 5 KB).
  - Upgrade `synthesize_wiki()` prompt to require an `architecture_mermaid` field containing a valid Mermaid diagram (`flowchart TD` / `flowchart LR` / `sequenceDiagram`, outer fences stripped on validation) depicting entrypoints, module boundaries, data flow, and state stores.
  - Update `format_wiki_section()` in `roles/base.py` to render markdown bullets for `technologies`/`concepts` and a ```` ```mermaid ```` block for `architecture_mermaid`, wrapped once via `wrap_untrusted(..., source="tenant_wiki")`.
  - In `run_onboarder()` (`roles/onboarder/runner.py`), skip `render_into()` and `publish_onboarding_pr()` when `int(os.environ.get("ISSUE_ID") or "0") == 0`.
- **Acceptance Criteria:**
  - [ ] **AC-1:** `wiki_parser.py`, `generate_wiki`, `_resolve_repo_path`, and `SotaWiki.to_markdown` are removed; `test_import_graph.py` passes.
  - [ ] **AC-2:** `synthesize_wiki()` validates `architecture_mermaid` (strips accidental markdown fences, retries if empty/invalid) and records `commit_sha`.
  - [ ] **AC-3:** `run_onboarder()` with `ISSUE_ID=0` synthesizes and upserts `SotaWiki` without checking out `atobar/onboarding` or opening a PR.

### WP-2: Supabase Migration & Coalesced Auto-Refresh Queue (`infra/supabase`, `contracts`, `flow-engine`)
- **Files:**
  - `infra/supabase/migrations/0026_wiki_lifecycle.sql`
  - `contracts/src/atobar_contracts/store.py`, `contracts/src/atobar_contracts/api.py`
  - `flow-engine/src/flow_engine/store.py`, `flow-engine/src/flow_engine/api.py`
- **Changes:**
  - Migration adds to `public.repository_wikis`:
    - `architecture_mermaid text not null default ''`
    - `commit_sha text not null default ''`
    - `refresh_status text not null default 'idle' check (refresh_status in ('idle', 'queued', 'running', 'failed'))`
    - `refresh_queued boolean not null default false`
    - `last_error text`
    - `merges_since_refresh integer not null default 0`
    - `refresh_interval_merges integer not null default 5 check (refresh_interval_merges >= 0 and refresh_interval_merges <= 100)`
  - Add atomic PL/pgSQL RPC `flow_record_default_branch_merge_for_wiki(p_tenant_id uuid, p_commit_sha text)` (increments `merges_since_refresh` when `commit_sha` changes and returns the updated row).
  - Add `_dispatch_or_queue_wiki_refresh(store, tenant, ec2_client)`:
    - If `store.active_session_for_role(tenant.id, "onboarder")` is non-None, set `refresh_queued = True`, `refresh_status = "queued"`, and return `{"status": "queued", "dispatched": False}`.
    - Otherwise set `refresh_status = "running"`, `refresh_queued = False`, mint an `onboarder` credential (`issue_id=0`), and call `launch_spot_instance`.
  - Hook into:
    1. `POST /internal/v1/tenants/{tenant_id}/wiki/regenerate` (manual Force Regenerate).
    2. `POST /internal/v1/tenants/{tenant_id}/wiki/policy` (updates `refresh_interval_merges`).
    3. `handle_post_metrics` (`api.py:1538`, when `body.pr_number is None`): atomically increment `merges_since_refresh`; if `refresh_interval_merges > 0` and `merges_since_refresh >= refresh_interval_merges`, call `_dispatch_or_queue_wiki_refresh`.
    4. `handle_run_turn_complete` (`api.py`): when an `onboarder` run completes or halts, set `refresh_status = 'idle'` (resets `merges_since_refresh = 0` on `POST /runs/{run_id}/wiki`) or `'failed'` (`last_error`); if `refresh_queued` is `True`, immediately dispatch one follow-up refresh.
- **Acceptance Criteria:**
  - [ ] **AC-4:** Concurrent regenerate requests while an `onboarder` session is active coalesce into `refresh_queued = True` without launching a second Spot VM, and automatically dispatch once when the active run finishes.
  - [ ] **AC-5:** Default-branch metric observations increment `merges_since_refresh` and auto-dispatch a refresh when `merges_since_refresh >= refresh_interval_merges` (and never when `refresh_interval_merges == 0`).

### WP-3: Control Panel Repo Configuration Screen (`control-panel`)
- **Files:**
  - `control-panel/src/control_panel/flow_engine_client.py`, `control-panel/src/control_panel/store.py`, `control-panel/src/control_panel/web.py`
- **Changes:**
  - Wire `regenerate_wiki` and `update_wiki_policy` in `FlowEngineClient` and `_build_local_fallback_transport`.
  - Add `_wiki_section(store, session, tenant, can_manage)` to `/tenants/{owner}/{repo}` in `control-panel/src/control_panel/web.py`:
    - Displays status pill (`idle` / `queued` / `running` / `failed`), `last_updated`, `commit_sha` permalink (`https://github.com/{owner}/{repo}/commit/{sha}`), and merge counter (`{merges_since_refresh} / {refresh_interval_merges} merges since refresh`).
    - Reconstructs and renders `purpose`, `technologies` (`<ul>`), `structure`, `concepts` (`<ul>`), and `architecture_mermaid` (`<pre class="mermaid"><code>...</code></pre>`) with **every** field passed through `html.escape()` (strictly XSS-safe under CP's `Content-Security-Policy`).
    - For repo admins (`can_manage`):
      - **Force Regenerate** button (`POST /tenants/{owner}/{repo}/wiki/regenerate`, CSRF-protected).
      - **Auto-Refresh Cadence** form (`POST /tenants/{owner}/{repo}/wiki/policy`, CSRF-protected, `refresh_interval_merges` input `0..100` where `0` = manual only).
      - Register both paths in `tenant_action_get_redirect` (`web.py:2100`).
- **Acceptance Criteria:**
  - [ ] **AC-6:** Repo viewers see the rendered wiki, Mermaid block, `commit_sha`, and refresh status on `/tenants/{owner}/{repo}`; malicious HTML/script tags in any wiki field are escaped in the response body.
  - [ ] **AC-7:** Repo admins can update `refresh_interval_merges` and trigger **Force Regenerate**; non-admins receive `403`.
  - [ ] **AC-8:** `uv run --no-sync atobar-ops verify` passes (`ruff`, combined `mypy`, `pytest -q`, schemas, `test_boundaries.py`, `test_table_ownership.py`).
