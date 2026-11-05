# J35: E2E Readiness & Execution Plan for `ollama-monitor#33` — Auth Redirects, Lazy Model Reload (`gemini-4-argon`), Vision Axiom Reconciliation & Tool-Level Token Telemetry

**Status:** Open · **Owner:** factory + human · **Target Tenant:** `arsenyspb/ollama-monitor` (Issue [#33](https://github.com/arsenyspb/ollama-monitor/issues/33))

---

## 1. Objective & Scope

Prepare and execute a clean, observable, end-to-end (`DEVELOPER` → `EVALUATOR` → `ARBITRATOR`) autonomous delegation over [`arsenyspb/ollama-monitor#33`](https://github.com/arsenyspb/ollama-monitor/issues/33) ("Add a `--version` flag to CLI") using the `nanobot` runtime. Before triggering the E2E run, resolve the six blocking gaps discovered in previous runs:

1. **WP-1 — Issue #33 & Repo Hygiene:** Purge all 29 lingering comments on `arsenyspb/ollama-monitor#33`, close open PR #44 (`flow/issue-33`), delete stale `flow/issue-33` and `flow-wip/*/33` branches, remove stale `flow:feature` / `flow:in-review` labels on `#33`, and clean stale `issue_id=33` / `pr_number=33` rows from `sessions`, `loop_rounds`, and `arbitration_decisions`.
2. **WP-2 — Control Panel Unauthenticated Redirect Flow (`/login?next=…`):** Fix `src/control_panel/web.py` so any unauthenticated request (both `GET` pages and `POST`/`GET` action routes such as `/tenants/arsenyspb/ollama-monitor/llm-keys/gemini/delete`) redirects via `303 See Other` to `/login?next=<target_page>`, preserves `next` across the GitHub OAuth round-trip (`/auth/callback`), and lands the user back on the originating page after authentication.
3. **WP-3 — Lazy-Reload of Provider Models on Dropdown Click & Select `gemini-4-argon`:**
   - Fix `_gemini_valid` in `src/control_panel/llm_keys.py` to paginate `GET https://generativelanguage.googleapis.com/v1beta/models` (`pageSize=1000` + `nextPageToken`) instead of truncating at Google's default 50-model page size (which hid `gemini-4-argon`, model #32 on page 2).
   - Add `GET /tenants/{owner}/{repo}/llm-models` in `src/control_panel/web.py` to decrypt the tenant's stored provider keys, re-fetch live model lists from the provider APIs, update `tenant_llm_keys.available_models` in Supabase, and return the fresh options as JSON.
   - Add `connect-src 'self'` to the Control Panel CSP header and attach a lazy-reload JS handler on `<select name='model'>` in `_role_llm_table` so clicking/focusing the dropdown refreshes available models in place.
   - Select `gemini-4-argon` for both `developer` and `evaluator` roles on `arsenyspb/ollama-monitor`.
4. **WP-4 — Research & Reconcile Multimodal Screenshot Code Path vs. SOTA'26 Axiom 7 & Axiom 11:** Audit how `inspect_ui` and `capture_evaluator_screenshot` execute inside the `nanobot` Spot instance vs. Axiom 7 ("Multimodal Vision Testing") and Axiom 11 ("Evaluator-Optimizer Separation"), fix immediate workspace/env defects, and stop to report architectural deviations for owner clarification.
5. **WP-5 — Harness Telemetry Wrapping with Per-Tool Token Breakdowns:** Wrap both `nanobot` (`FlowHook` in `src/flow_engine/agents/nanobot_runtime.py`) and `native` (`src/flow_engine/runtime/llm_loop.py`) plus `tools_mcp/server.py` so token usage (`input_tokens`, `output_tokens`, `cached_input_tokens`, `total_tokens`, `args_tokens`, `result_tokens`, `cost_usd`, `calls`, `duration_ms`, `errors`) is tracked and broken down per tool, persisted to `sessions.usage` and `loop_rounds.usage_detail`, and rendered in `GitHubTelemetryUI` and the Control Panel PR timeline.
6. **WP-6 — E2E Pre-Flight Verification & Trigger Readiness on `ollama-monitor#33`:** Fix the Arbitrator baseline observation window in `GET /v1/runs/{run_id}/arbitration-data` (so default-branch `pr_number IS NULL` baseline rows are never evicted by >50 PR observations), verify all pre-flight checks, and report readiness to trigger `#33`.

---

## 2. `<self_doubt>`: Arguing With Ourselves Over Every Step of One E2E Delegation

<self_doubt>

### Q1: If we only delete comments on `ollama-monitor#33` and close PR #44, will a new `flow:feature` label cleanly trigger a fresh E2E run?
**Argument against naive cleanup:**
No! Three hidden state traps would still break or pollute the run:
1. **GitHub label idempotency:** Issue #33 currently already has the labels `flow:feature` and `flow:in-review` attached. In GitHub, re-applying a label that is already present does **not** fire `issues.labeled`. We must remove `flow:feature` and `flow:in-review` first so applying `flow:feature` fires a fresh webhook.
2. **Remote Git branches (`flow/issue-33` and `flow-wip/*/33`):** When `create_and_verify_pr` runs on the new Developer Spot instance, or if `WipSyncer` checks for prior branches, leftover `flow/issue-33` or `flow-wip/developer/33` branches can cause non-fast-forward push rejections or stale commit history. All four branches (`flow/issue-33`, `flow-wip/developer/33`, `flow-wip/evaluator/33`, `flow-wip/arbitrator/33`) must be deleted via `DELETE /repos/arsenyspb/ollama-monitor/git/refs/heads/...`.
3. **Supabase `sessions` and `loop_rounds`:** `ingest_event` in `src/control_panel/app.py` checks `store.issue_session_active(tenant.id, 33)`, and the Control Panel UI aggregates `loop_rounds` by `pr_number=33` (which currently has 33 old rows from Oct 10–13, including 5 reaped rows with `head_sha=0000...`). Purging old `issue_id=33` / `pr_number=33` records in `sessions`, `loop_rounds`, and `arbitration_decisions` ensures clean accounting for the new E2E run.

### Q2: Why did `https://$CP_HOSTNAME/tenants/arsenyspb/ollama-monitor/llm-keys/gemini/delete` fail when unauthenticated, and what happens if we redirect a `POST` route to `/login?next=/tenants/.../delete`?
**Argument against redirecting back to `/delete` after login:**
When a user's session cookie (`cp_session`) expires while viewing `/tenants/arsenyspb/ollama-monitor` and they click "Remove" on a key, the browser sends `POST /tenants/arsenyspb/ollama-monitor/llm-keys/gemini/delete`.
- Currently, `_form()` raises `HTTPException(401, "sign in first")`, leaving the browser stuck on the URL `.../llm-keys/gemini/delete` showing raw JSON `{"detail":"sign in first"}`.
- If the user refreshes that URL in the address bar, the browser sends `GET /tenants/arsenyspb/ollama-monitor/llm-keys/gemini/delete`, which currently returns `405 Method Not Allowed` because only `@router.post` is registered!
- Furthermore, OAuth callback (`/auth/callback`) completes with a browser `GET` redirect (`303 See Other`). If `next` were set to `/tenants/arsenyspb/ollama-monitor/llm-keys/gemini/delete`, then after logging in, the browser would `GET` the `/delete` endpoint!
- **Resolution:**
  1. Never perform destructive mutations on `GET`.
  2. For any action sub-route under `/tenants/{owner}/{repo}/...` (whether reached via unauthenticated `POST` or any `GET`), the canonical `next` page is `/tenants/{owner}/{repo}`.
  3. If an unauthenticated user hits `POST` or `GET` on `/tenants/{owner}/{repo}/llm-keys/{provider}/delete` (or `/token`, `/llm-keys`, `/arbitration-mode`, `/llm-roles/{role}`), redirect (`303 See Other`) to `/login?next=/tenants/{owner}/{repo}`. If an authenticated user hits `GET` on those action sub-routes, redirect (`303 See Other`) to `/tenants/{owner}/{repo}`.
  4. For any `GET` page (`/tenants/{owner}/{repo}`, `/tenants/{owner}/{repo}/prs/{pr_number}`, etc.), when `session is None`, redirect to `/login?next=<request.url.path>`.
  5. Validate `next` with `_safe_next()` (`next.startswith("/") and not next.startswith("//")`) and bind it inside the signed/verified OAuth state cookie so open redirects are impossible.

### Q3: Why wasn't "Argon" (`gemini-4-argon`) visible in the Control Panel dropdown, and will a client-side lazy-reload alone work if we don't also update the backend and CSP?
**Argument against client-side-only dropdown fetch:**
1. **Why `gemini-4-argon` was missing:** `_gemini_valid()` in `src/control_panel/llm_keys.py` called `GET https://generativelanguage.googleapis.com/v1beta/models` with no `pageSize` parameter. Google's API defaults to `pageSize=50` and paginates via `nextPageToken`. There are currently 63 models on `v1beta/models`, and `gemini-4-argon` (`Gemini 4 Argon`) was truncated or added after the initial 50-model snapshot was saved to `tenant_llm_keys.available_models`!
2. **CSP trap:** `_page()` in `src/control_panel/web.py` sets `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'self' https://github.com`. Because `default-src` is `'none'` and `connect-src` is omitted, any `fetch()` call from JavaScript when clicking the dropdown will be **blocked by the browser's Content Security Policy** unless we add `connect-src 'self'`!
3. **Backend validation trap:** When the user selects a newly lazy-loaded model from the dropdown, `onchange='this.form.submit()'` posts to `POST /tenants/{owner}/{repo}/llm-roles/{role}`, which checks:
   ```python
   if (provider, model) not in _model_choices(store.llm_keys(tenant.id)):
       raise HTTPException(400, "pick a model offered by one of this repository's keys")
   ```
   If the lazy-reload endpoint only returned models to the browser without persisting the refreshed `available_models` list into `tenant_llm_keys` in Supabase, selecting a newly discovered model would fail with HTTP 400!
- **Resolution:**
  - Fix `_gemini_valid()` to paginate with `pageSize=1000` + `nextPageToken`.
  - Add `GET /tenants/{owner}/{repo}/llm-models` which decrypts each configured key for the tenant, calls `provider.validate(api_key)`, updates `available_models` in `tenant_llm_keys`, and returns the fresh `[{provider, model, label}]` list.
  - Add `connect-src 'self'` to `_page()` CSP.
  - Wire `<select name='model'>` in `_role_llm_table` to lazily call `/tenants/{owner}/{repo}/llm-models` on click/focus (`onmousedown`/`onfocus`), preserving the selected option and populating any new models without losing focus.
  - Immediately refresh `arsenyspb/ollama-monitor`'s `available_models` in Supabase and set both `developer` and `evaluator` role preferences to `gemini` / `gemini-4-argon`.

### Q4: Does the screenshot code path in `nanobot` actually conform to SOTA'26 Axiom 7 and Axiom 11?
**Argument from real execution evidence (`run_ddaaeb54275d3e1e` on `ollama-monitor`):**
No! In our audit of the codebase and the actual `run_events` from `run_ddaaeb54275d3e1e`, we found **five concrete deviations** between Axiom 7 / Axiom 11 and what executes inside a `nanobot` Spot instance:
1. **Deviation A — `inspect_ui` returns a >100KB JSON base64 text string over MCP instead of multimodal pixels:**
   - In `native` (`src/flow_engine/runtime/llm_loop.py:163-179`), `llm_loop` had a special-case check `if isinstance(result, dict) and "screenshot_base64" in result:` that decoded the PNG bytes and injected `{"data": image_bytes, "mime_type": "image/png"}` into the LLM message.
   - In `nanobot` (`src/flow_engine/tools_mcp/server.py`), `inspect_ui` is exposed over FastMCP returning `dict[str, str]`. FastMCP serializes that dict as `TextContent` JSON. `nanobot`'s `ContextGovernor` sees a >100KB text result, truncates it, and spills it to `.nanobot/tool-results/<session>/call_<id>.txt`. The LLM **never** receives multimodal image tokens from `inspect_ui`! In `run_ddaaeb54275d3e1e`, the Evaluator had to manually run a Python one-liner via `execute_command` to read `.nanobot/tool-results/.../call_425849.txt` and decode `screenshot_base64` into `screenshot.png`!
2. **Deviation B — MCP server working directory puts `.atobar/artifacts` outside the `nanobot` workspace:**
   - `render_nanobot_run_config` (`src/flow_engine/agents/nanobot_config.py:124`) launches the MCP server with `uv run --directory /home/ubuntu/atobar-flow python -m flow_engine.tools_mcp.server`.
   - Consequently, `DEFAULT_ARTIFACTS_DIR = Path(".atobar/artifacts")` in `vision_tools.py` resolves to `/home/ubuntu/atobar-flow/.atobar/artifacts/<sha256>.png`, **outside** `/home/ubuntu/workspace`.
   - Because `nanobot` runs with `restrictToWorkspace: True`, `nanobot`'s built-in `read_file` tool (which **does** support native multimodal image ingestion via `build_image_content_blocks`!) is forbidden from reading `/home/ubuntu/atobar-flow/.atobar/artifacts/<sha256>.png`!
3. **Deviation C — `DISPLAY=:0` is missing from MCP server environment & PTY subshells:**
   - `render_nanobot_run_config` (`nanobot_config.py:134-150`) filters `env` for the `flow` MCP server to an explicit allowlist that omits `DISPLAY`. In `run_ddaaeb54275d3e1e`, `echo "DISPLAY=$DISPLAY"` returned empty `DISPLAY=`.
4. **Deviation D — No X11 terminal emulator (`xterm`) on the AMI for TUI applications:**
   - `infra/packer/scripts/install_deps.sh` installs `tigervnc-standalone-server websockify chromium openbox fonts-liberation`, and boots Chromium on `:0`. It installs **no X11 terminal emulator** (`xterm`, `kitty`, `alacritty`, etc.).
   - Yet `EVALUATOR_SYSTEM_PROMPT` (`src/flow_engine/roles/evaluator/prompt.py:22-25`) instructs: *"If the pull request modifies visual rendering, layout, or Terminal UI (TUI) charts: You MUST run the UI application and call `capture_evaluator_screenshot`."*
   - When evaluating a TUI app like `ollama-monitor`, running the TUI inside `execute_command` runs in a headless Python `pty.openpty()` slave (`pty_manager.py`), which does **not** render onto X11 `DISPLAY=:0`! In `run_ddaaeb54275d3e1e`, the Evaluator discovered no `xterm` existed on the machine and had to convert the TUI output to HTML and open it in `DISPLAY=:0 chromium` just to put pixels on `:0`!
5. **Deviation E — Visual artifacts (`artifact:sha256:…`) are never persisted off the ephemeral Spot instance:**
   - `capture_evaluator_screenshot` writes `<sha256>.png` to local disk only. Seconds after `flow-verdict` is posted, `_shutdown()` runs `sudo shutdown -h now` and AWS terminates the Spot instance, destroying the PNG file. Only the `sha256` hash survives in `run_events` and the `flow-verdict` comment.

### Q5: Why were `sessions.usage` and `loop_rounds.usage_detail` always `None` / `0` in Supabase, and why were ~98% of tool calls missing from `run_events` on `nanobot`?
**Argument from `nanobot_runtime.py`, `conductor/io.py`, and `run_events` inspection:**
1. **Missing `nanobot` builtin tool telemetry:** `_wrap_tool` in `src/flow_engine/tools_mcp/server.py` only wraps MCP tools (`mcp_flow_*`). When `nanobot` runs, it maps allowed flow tools onto its own built-in tools (`exec`, `read_file`, `write_file`, `edit_file`, `apply_patch`, `grep`, `list_dir`, `find_files`). In `run_c759993cdfa0a18c` (154 iterations), 152 tool calls used `nanobot` built-ins and only 2 used MCP tools! Because `FlowHook` did not implement `before_execute_tool` / `after_execute_tool` / `on_execute_tool_error`, all built-in tool executions were unmeasured.
2. **`FlowHook` never updated `SessionRegistry.usage`:** `FlowHook.after_iteration` appended `ModelCallRecord`s to `self.recorded_calls` and updated `telemetry_ui`, but **never** updated `self.hooks.registry.usage`, `cost_estimate`, or `iterations`. And `run_developer` / `run_evaluator` never wrote `result.usage` to `registry` before posting the `flow-ack` / `flow-verdict` envelope.
3. **`conductor/io.py` dropped `usage_detail`:** In `process_flow_event` (`src/flow_engine/conductor/io.py:421-453`), even when reading `sess.get("usage")`, `round_row` only set `tokens` and `cost_usd` and omitted `"usage_detail": sess_usage`!
4. **No per-tool token breakdown:** Neither `FlowHook` nor `llm_loop` attributed model turn tokens (`input_tokens`, `output_tokens`, `cached_input_tokens`, `cost_usd`) or tool payload tokens (`args_tokens`, `result_tokens`) to individual tools.

### Q6: If Developer and Evaluator succeed on `ollama-monitor#33`, will Arbitrator pass?
**Argument from `run_45dba2460e529624` (Arbitrator on PR #47):**
In `run_45dba2460e529624`, the Arbitrator ran in `shadow` mode and reported:
`Insufficient baseline samples for 'dependency_count'`.
Why? Because `get_arbitration_data` in `src/control_panel/app.py` calls `store.recent_observations(tenant_id)` which has `limit=50`. After 70+ PR observations accumulated in `metric_observations`, the 2 default-branch baseline observations (`id=3, 4`, `pr_number=None`) were pushed past row 50!
- **Resolution:** Update `get_arbitration_data` in `src/control_panel/app.py` (and `store.py`) so default-branch baseline observations (`pr_number is None`) are always fetched alongside recent PR observations.

</self_doubt>

---

## 3. Work Packages & Execution Plan

### WP-1: Clean `arsenyspb/ollama-monitor#33` & Related PRs/Branches/DB State
- [x] Delete all 29 lingering comments on `arsenyspb/ollama-monitor#33`.
- [x] Close open PR #44 (`flow/issue-33`) on `arsenyspb/ollama-monitor`.
- [x] Delete remote branches `flow/issue-33`, `flow-wip/developer/33`, `flow-wip/evaluator/33`, `flow-wip/arbitrator/33` on `arsenyspb/ollama-monitor`.
- [x] Remove labels `flow:feature` and `flow:in-review` from `arsenyspb/ollama-monitor#33`.
- [x] Clean stale `issue_id=33` / `pr_number=33` rows from `sessions`, `loop_rounds`, and `arbitration_decisions` in Supabase.

### WP-2: Automatic Login Redirects (`src/control_panel/web.py`)
- [x] Write failing tests in `tests/control_panel/test_web.py` covering:
  - Unauthenticated `GET /tenants/arsenyspb/ollama-monitor` → `303` to `/login?next=%2Ftenants%2Farsenyspb%2Follama-monitor`.
  - Unauthenticated `GET /tenants/arsenyspb/ollama-monitor/prs/33` → `303` to `/login?next=%2Ftenants%2Farsenyspb%2Follama-monitor%2Fprs%2F33`.
  - Unauthenticated `POST /tenants/arsenyspb/ollama-monitor/llm-keys/gemini/delete` (and all other POST routes) → `303` to `/login?next=%2Ftenants%2Farsenyspb%2Follama-monitor`.
  - Unauthenticated `GET /tenants/arsenyspb/ollama-monitor/llm-keys/gemini/delete` → `303` to `/login?next=%2Ftenants%2Farsenyspb%2Follama-monitor`; authenticated `GET` on action sub-paths → `303` to `/tenants/arsenyspb/ollama-monitor`.
  - `/login?next=...` → `/auth/callback` redirects back to the validated `next` path (and rejects external/protocol-relative URLs like `//evil.com` or `https://evil.com`).
- [x] Implement in `src/control_panel/web.py`.

### WP-3: Lazy-Reload Models on Dropdown Click & Select `gemini-4-argon`
- [x] Write failing tests in `tests/control_panel/test_llm_keys.py` and `tests/control_panel/test_web.py` covering:
  - Multi-page pagination (`nextPageToken`) in `_gemini_valid`.
  - `GET /tenants/{owner}/{repo}/llm-models` decrypting the tenant's key, refreshing `available_models` from the provider, persisting the updated list in `store`, and returning JSON `{models: [...]}`.
  - `<select name='model'>` in `_role_llm_table` rendering `data-models-url` and lazy-reload script + `connect-src 'self'` in CSP.
- [x] Implement in `src/control_panel/llm_keys.py` and `src/control_panel/web.py`.
- [x] Refresh `available_models` (all 63 models including `gemini-4-argon`) and set `developer` and `evaluator` role models to `gemini-4-argon` for `arsenyspb/ollama-monitor` in Supabase.

### WP-4: Reconcile Screenshot Capture Path (`vision_tools.py`, `nanobot_config.py`) & Report Findings
- [x] Fix `DEFAULT_ARTIFACTS_DIR` resolution in `src/flow_engine/tools/vision_tools.py` to use `WORKSPACE_DIR/.atobar/artifacts` when `WORKSPACE_DIR` is set (so artifacts are saved inside the workspace where `nanobot`'s multimodal `read_file` can read them).
- [x] Update `inspect_ui()` in `src/flow_engine/tools/vision_tools.py` so it also records the screenshot artifact via `record_visual_artifact` inside the workspace and includes `artifact_path` and `sha256` in its return payload.
- [x] Pass `DISPLAY` (`:0` default) into `mcpServers.flow.env` in `src/flow_engine/agents/nanobot_config.py` and set `cwd: str(agent_ws)` on the MCP server config.
- [x] Stop and report the architectural deviations (Axiom 7 vs. `nanobot` MCP tool output, TUI rendering on `:0` without `xterm`, and ephemeral Spot artifact persistence) to the owner for clarification.

### WP-5: Harness Telemetry Wrapping with Per-Tool Token Breakdowns
- [x] Write failing tests in `tests/agents/test_nanobot_runtime.py`, `tests/telemetry/test_github_ui.py`, and `tests/conductor/test_io.py` verifying:
  - Per-tool token and call breakdown (`by_tool`) in `UsageLedger` and `FlowHook` (covering both `nanobot` built-in tools like `exec`/`read_file` and MCP tools like `mcp_flow_todo_write`).
  - `SessionRegistry` receives updated `usage` (with `total` and `by_tool`), `cost_estimate`, and `iterations` during the run and before envelope publication.
  - `GitHubTelemetryUI.format_payload` renders total tokens and the per-tool token breakdown.
  - `conductor/io.py` snapshots `usage_detail` (including `by_tool`) into `loop_rounds`.
- [x] Implement across `src/flow_engine/agents/base.py`, `src/flow_engine/agents/nanobot_runtime.py`, `src/flow_engine/runtime/llm_loop.py`, `src/flow_engine/tools_mcp/server.py`, `src/flow_engine/roles/developer/runner.py`, `src/flow_engine/roles/evaluator/runner.py`, `src/flow_engine/telemetry/github_ui.py`, `src/control_panel/web.py`, and `src/flow_engine/conductor/io.py`.

### WP-6: Arbitrator Baseline Fix & E2E Pre-Flight Verification
- [x] Update `get_arbitration_data` in `src/control_panel/app.py` and `store.py` so default-branch (`pr_number is None`) baseline observations are always included alongside recent PR observations.
- [x] Run full verification suite (`ruff check src/ tests/`, `mypy src/ --ignore-missing-imports`, `PYTHONPATH=src pytest tests/ -q`).
- [x] Report to owner with WP-4 clarifications and E2E trigger readiness for `arsenyspb/ollama-monitor#33`.
