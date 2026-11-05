# J49 (conversational-pr-steering-and-screenshot-provenance): Unified `@atobar-flow-agent` Issue (= Plan) & PR (= Correction) Steering, Actionable Evaluator UX, and Verifiable Screenshot Provenance

## Goal
Enable frictionless human-in-the-loop steering and verifiable visual evidence across every tenant Issue and Pull Request:
1. **Unified `@atobar-flow-agent` Conversational Steering (Issue = Plan, PR = Correction):** Allow trusted repository collaborators to comment `@atobar-flow-agent <instructions>` (or `@atobar` / `/flow`) in **both** Issues and Pull Requests using identical trust, deduplication, and routing mechanics — where commenting on an **Issue** refines or approves the **Plan** (`planner` $\rightarrow$ `developer`), and commenting on a **PR** triggers **Correction** (`developer`) or **Re-Evaluation** (`evaluator`) on that PR's `head_sha`.
2. **Actionable Evaluator Verdict UX on PRs & Issues:** Ensure the Evaluator's review comment is always posted to the open PR (even when 0 screenshots were recorded) and ends with a state-aware **"Your Next Action"** call-to-action guiding the user on how to merge, request code corrections, or trigger a targeted re-evaluation.
3. **Verifiable Screenshot Provenance, Constraints Reporting & Anti-Hallucination Hardening:** Require every Developer and Evaluator visual capture (`inspect_ui`, `capture_evaluator_screenshot`) to declare its **data mode (`live` | `synthetic` | `hybrid`), data source, environment constraints, and tested scenario**, combine this with OS-collected X11 window/process telemetry (`xwininfo`, `ps`, `sha256`), persist the full entry in Supabase `run_events`, and render a human-readable `<details>` provenance fold-up directly beneath every screenshot in GitHub.
4. **Fix Sandbox Terminal & `artifacts_dir` Capture Bugs:** Guarantee `xterm` and `x11-utils` (`xwininfo`) are installed in both the Packer AMI and Spot Phase-0 boot, eliminate the `artifacts_dir` path split between MCP tools and `workspace_dir()`, filter out blank unmapped-window captures, and steer prompts away from smooth `math.sin` demo curves.

## FINAL VERDICT AND TODO
- [x] **WP-1:** Client workflow (`flow-trigger.yml`) & Conductor PR-to-Issue resolution for `@atobar-flow-agent` / `@atobar` / `/flow` comments and PR reviews
- [x] **WP-2:** Deterministic `HumanCommentEvent` state machine routing (**Issue = Plan**, **PR = Correction / Re-Eval**), epoch auto-reset, `👀` reaction ack, and `_run_serverless_arbitrator` envelope claim fix
- [x] **WP-3:** Dual-thread (`Issue` + `PR`) trusted human comment collection (`_human_comments_for`) wired into `planner`, `developer`, and `evaluator` contexts + actionable Evaluator "Your Next Action" PR UX
- [x] **WP-4:** Screenshot provenance metadata (`data_mode`, `data_source`, `constraints`, `scenario` + OS `xwininfo`/`ps` attestation) in `vision_tools.py`, Control Panel `/v1/runs/{run_id}/screenshots`, Supabase `run_events`, and PR `<details>` fold-up summary
- [x] **WP-5:** Fix `artifacts_dir` / `FLOW_WORKSPACE_DIR` resolution bug, blank X11 desktop capture filter (`has_active_window`), and `xterm` + `x11-utils` installation in Packer AMI + Spot `cloud_init.sh.tpl`
- [x] **WP-6:** Developer & Evaluator prompt steering (`developer/prompt.py`, `evaluator/prompt.py`, `SOUL.md`) enforcing Live-First execution, realistic multi-regime synthetic traces (banning uniform `math.sin` curves), and Evaluator fixture independence

---

## Part 1 — Forensic Root-Cause Analysis (verified on `arsenyspb/ollama-monitor#53` / `pull/54` & `c0ae936`)

### 1.1 Why `@atobar-flow-agent` Cannot Be Tagged Like `@claude` on PR #54, and the 4 Codebase Gates Blocking PR Comments
1. **GitHub Platform Identity Difference (`User` vs. `Bot`):**
   - Querying `gh api users/claude` returns `{"id": 81847, "login": "claude", "type": "User"}` — `claude` is a registered GitHub `User` account, so GitHub's markdown parser and `@mention` resolver link `@claude`.
   - Querying `gh api users/atobar-flow-agent` returns `404 Not Found`. `atobar-flow-agent` exists only as a **GitHub App slug** (`/apps/atobar-flow-agent`), whose actor login is `atobar-flow-agent[bot]` (`type: "Bot"`). GitHub's comment box `@` autocomplete dropdown (`/suggestions/issue/<id>`) excludes `type: "Bot"` accounts, and typing `[` in `@atobar-flow-agent[bot]` terminates the `@` mention token.
   - Even for `@claude`, Claude's GitHub Action does not rely on GitHub App notifications; its workflow triggers on `issue_comment` / `pull_request_review` and string-matches `@claude` in the comment body.
2. **Four Sequential Codebase Gates Currently Dropping Human PR Comments:**
   - **Gate 1 — Client Workflow Filter (`contracts/src/atobar_contracts/client_templates.py:42-43`):** `render_trigger()` gates `issue_comment` on `contains(github.event.comment.body, '```flow-')`. Plain human comments and `@atobar-flow-agent` mentions are dropped in GitHub Actions and never reach `POST /v1/events`.
   - **Gate 2 — PR Number (`#54`) vs. Delegated Issue Number (`#53`) Split (`client_templates.py:56,95`, `flow-engine/src/flow_engine/api.py:1187`):** `LoopState`, `loop_rounds`, and `processed_events` are keyed by the delegated Issue number (`#53`), whereas an `issue_comment` on PR `#54` sends `issue_number = 54`. Only `handle_merged_pr_event()` (`conductor_io.py:640-699`) currently resolves a PR number back to its parent issue.
   - **Gate 3 — Conductor Rejects Non-Envelope Comments (`contracts/src/atobar_contracts/conductor_io.py:1020-1111`):** `process_flow_event()` runs `envelope = parse(body, ...)`, which returns `None` for any non-envelope comment and exits with `{"status": "ignored", "reason": "comment is not a valid envelope"}`.
   - **Gate 4 — No `HumanCommentEvent` in `machine.py` & No PR Comments in `evaluator` (`contracts/src/atobar_contracts/machine.py:72-80`, `flow-agent/src/flow_agent/main.py:125-184`):** `machine.py` only accepts `HumanLabelEvent` (`flow:approved`, `flow:resume`), and `_human_comments_for()` only reads comments from `issue_id` (`#53`) and only passes them to `run_developer()`, never to `run_evaluator()` or `run_planner()`.

### 1.2 Evaluator PR Comment UX Gap & Post-Convergence Escalation Bug on `#53`
1. **Bare PR Comment UX (`flow-agent/src/flow_agent/roles/evaluator/spec.py:258-283`):**
   - `post_evaluator_verdict()` posts `format_evaluator_review_summary(verdict)` on the **Issue** (`#53`), and only posts a minimal screenshot mirror on the **PR** (`#54`) gated by `if md_lines:`. When 0 screenshots are recorded, the Evaluator posts nothing on the PR at all.
   - Neither the PR comment nor the Issue review comment includes a contextual call-to-action explaining what the user should do next (merge after `flow/arbiter`, comment `@atobar-flow-agent <correction>` to trigger Developer rework, or comment `@atobar-flow-agent evaluate <check>` to re-run Evaluator).
2. **Serverless Arbitrator Unclaimed Envelope Race (`contracts/src/atobar_contracts/conductor_io.py:429-455`):**
   - On `ollama-monitor#53`, Evaluator approved Round 1 at `11:11:58Z` and serverless Arbitrator posted `flow-arbitration: pass` (`comment_id=6363570061`) at `11:12:19Z`. However, `_run_serverless_arbitrator()` did not call `store.claim_run_credential_envelope(run_id, comment_id)`. When a subsequent webhook arrived at `11:12:38Z`, `rebuild_loop_state()` (`conductor_io.py:242-254`) logged `"Ignoring unclaimed envelope for post-migration run_id run_92d2d14de2dd51a8"`, treated `latest_arbitration` as `None`, and tripped `max_pr_wall_clock_minutes` (`33.61 >= 30`), falsely escalating a PR that had already passed!

### 1.3 Forensic Audit of PR #54 Screenshots (`qwen3-coder:30b` & Sinewaves)
Inspection of Supabase `run_events` for Developer run `run_79380f3368d6c33d` and Evaluator run `run_f75238a386634452` revealed:
1. **Real X11 TUI Renders Driven by Unlabeled Synthetic Sinewave CSVs:**
   - The Spot VMs (`t4g.large`, 2 vCPU ARM64, 8 GB RAM, no GPU, Ubuntu Linux) cannot run a 30B local Ollama model (`18.5 GB` VRAM) or macOS `powermetrics`.
   - At `seq=1792753036092872944`, the Developer generated `/tmp/demo_monitor.csv` using `math.sin` / `math.cos` and hardcoded `qwen3-coder:30b,30B|Q4_K_M|18.4GB`, launched `src/monitor_tui.py` inside `zutty` on `DISPLAY=:0` (`seq=1792753213650625171`), and captured `d4f5205c2ced202a.png` via `inspect_ui` (`seq=1792753226667783521`).
   - At `seq=1792753668462487420`, the Evaluator anchored on the Developer's PR diff/description and generated `/tmp/eval_monitor_metrics.csv` using pure `math.sin` / `math.cos` and `qwen3-coder:30b,30B|Q4_K_M|18.5GB`.
2. **Missing `xterm` on Spot AMI + Blank Desktop Capture (`7562365711223610.png`):**
   - Both prompts instruct agents to run `DISPLAY=:0 xterm ...`, which failed on both VMs with `Command 'xterm' not found` (only `/usr/bin/zutty` was installed on the pre-baked AMI).
   - In `run_f75238a386634452`, the Evaluator called `inspect_ui` (`seq=1792753745566854397`) *after* `xterm` failed and *before* launching `zutty` (`seq=1792753784809991388`), recording a blank desktop screenshot (`7562365711223610.png`) into the worktree's `.atobar/artifacts/screenshots.json`.
3. **`artifacts_dir` Split Bug (`6bfa635e82ba4285.png` Dropped from PR #54):**
   - After launching `zutty`, the Evaluator called `capture_evaluator_screenshot(artifacts_dir='/home/ubuntu/flow-run/run_f75238a386634452/agent_ws/artifacts')` (`seq=1792753824420318445`).
   - Because `resolve_default_artifacts_dir()` in `vision_tools.py:20-25` only read `WORKSPACE_DIR` (ignoring `FLOW_WORKSPACE_DIR` used by `git_tools.py:workspace_dir()`) and allowed the LLM to override `artifacts_dir`, the valid TUI capture (`6bfa635e82ba4285.png`) was written outside `workspace_dir()`, so `post_evaluator_verdict()` only attached the earlier `7562365711223610.png` URL to PR #54.

---

## Part 2 — Architectural & Implementation Choices (With Reasoning)

| Decision Area | Chosen Approach | Rejected Alternative | Reasoning |
| :--- | :--- | :--- | :--- |
| **1. Triggering on `@atobar-flow-agent` in PRs & Issues** | Parse `@atobar-flow-agent`, `@atobar`, and `/flow` case-insensitively in `flow-trigger.yml` (`issue_comment` + `pull_request_review`) and Conductor, plus optional read-only `User` collaborator for UI autocomplete. | Relying on GitHub App `[bot]` mention notifications or label-only triggers. | GitHub excludes `type: "Bot"` (`*[bot]`) accounts from comment `@` autocomplete and `[` breaks `@mention` tokenization. String-matching in `issue_comment` / `pull_request_review` (the same mechanism used by `@claude` in `claude-code-action`) works deterministically today with zero extra credentials. |
| **2. Issue vs. PR Comment Semantics** | **Location-based default routing (`Issue = Plan`, `PR = Correction`)** with explicit keyword overrides (`approve`/`implement` on Issue $\rightarrow$ `developer`; `evaluate`/`review` on PR $\rightarrow$ `evaluator`). | Requiring users to memorize separate slash commands (`/flow-plan`, `/flow-dev`, `/flow-eval`) or apply labels on the Issue while reviewing a PR. | Matches how engineers naturally use GitHub: comments on an Issue discuss *what to build* (Plan), while comments on a PR discuss *what to fix or verify in the diff* (Correction/Review). |
| **3. PR-to-Issue State Resolution** | Keep canonical `LoopState` keyed by the delegated `issue_number` (`#53`) and resolve incoming PR events (`#54`) via branch (`flow/issue-<N>` / `flow-<N>`), PR body (`Fixes #<N>`), or `loop_rounds.head_sha`. | Creating a separate `LoopState` row keyed by `pr_number` (`#54`). | Keying by `issue_number` preserves a single continuous budget, round counter, and telemetry audit trail from `planner` $\rightarrow$ `developer` $\rightarrow$ `evaluator` $\rightarrow$ `arbitrator`. Splitting state across `#53` and `#54` would bifurcate cost/round caps. |
| **4. Handling Comments on `escalated` or `converged` Runs** | A trusted human `@atobar-flow-agent` comment on an `escalated` or `converged` Issue/PR automatically mints a fresh server-signed `<!-- flow:loop-epoch -->` marker and dispatches the target role. | Forcing the user to comment on PR #54, then navigate back to Issue #53 and manually apply the `flow:resume` label. | Requiring a cross-page label click after typing a `@atobar-flow-agent` correction comment on a PR breaks conversational flow. Because author trust (`OWNER`/`MEMBER`/`COLLABORATOR`) is already verified, an explicit `@atobar-flow-agent` directive is an authenticated human resume signal. |
| **5. Visual Verification Realism & Constraints Reporting** | Require structured `data_mode` (`live` \| `synthetic` \| `hybrid`), `data_source`, `constraints`, and `scenario` arguments on screenshot tools + OS-level `xwininfo`/`ps` capture + prompt ban on `math.sin` curves. | Forcing agents to install heavy external daemons/models (e.g. Ollama + 30B weights) or banning synthetic data entirely. | A `t4g.large` (8 GB RAM, no GPU) Spot VM physically cannot run a 30B model or macOS `powermetrics`. Requiring explicit constraint declaration + realistic multi-regime synthetic traces (idle, step-burst, jitter, spike, drop-off) + OS process attestation makes synthetic vs. live testing 100% transparent and catches real rendering edge cases. |
| **6. Blank X11 Capture & `artifacts_dir` Handling** | Remove `artifacts_dir` from MCP tool schema, resolve via `git_tools.workspace_dir()`, record `has_active_window` via `xwininfo`, and filter out `has_active_window=False` captures when a valid capture exists. | Failing the entire agent turn when `xwininfo` sees no window on `DISPLAY=:0`. | Returning a non-fatal tool warning (`"No active X11 window detected"`) lets the agent fix its background command and re-capture within the same turn, while filtering out the earlier blank capture prevents broken intermediate screenshots from appearing on the PR. |

---

## Part 3 — Work Packages & Implementation Expectations

### WP-1: Client Workflow (`flow-trigger.yml`) & PR-to-Issue Resolution
**Files:** `contracts/src/atobar_contracts/client_templates.py`, `contracts/src/atobar_contracts/conductor_io.py`, `flow-engine/src/flow_engine/api.py`, `control-panel/src/control_panel/app.py`

1. **`render_trigger()` in `contracts/src/atobar_contracts/client_templates.py`:**
   - Add `pull_request_review: types: [submitted]` and `pull_request_review_comment: types: [created]` alongside `issue_comment: types: [created]`.
   - Update the job `if:` condition so comment/review events forward when:
     - Sender does not end with `[bot]`, AND
     - Comment body contains ` ```flow- `, OR case-insensitively matches `@atobar-flow-agent`, `@atobar`, or `/flow`.
   - Include `"is_pull_request": true/false` and `"author_association"` in the JSON payload posted to `POST /v1/events`.
2. **`resolve_delegated_issue_number()` in `contracts/src/atobar_contracts/conductor_io.py`:**
   - Extract and generalize the PR-to-Issue resolution logic from `handle_merged_pr_event()` (`conductor_io.py:640-699`):
     - Match head branch `^flow[-/](?:issue[-/])?(\d+)(?:\b|[-_/])` (covering both `flow-53-...` and `flow/issue-53`).
     - Fallback to `(?:Fixes|Closes|Resolves)\s+#(\d+)\b` in the PR body.
     - Fallback to matching `head_sha` in `store.loop_rounds_for_tenant(tenant.id)`.
   - When an event arrives on PR `#54`, resolve `delegated_issue_number = 53` for `LoopState` while preserving `source_pr_number = 54` and the PR's `head_sha`.

### WP-2: `HumanCommentEvent` State Machine Routing, Auto-Epoch Reset & Arbitrator Claim Fix
**Files:** `contracts/src/atobar_contracts/machine.py`, `contracts/src/atobar_contracts/conductor_io.py`, `contracts/src/atobar_contracts/github_rest.py`

1. **`HumanCommentEvent` in `contracts/src/atobar_contracts/machine.py`:**
   ```python
   class HumanCommentEvent(BaseModel):
       model_config = ConfigDict(extra="forbid")
       target: Literal["issue", "pr"]
       intent: Literal["plan", "approve", "correct", "evaluate"]
       head_sha: str = ""
       comment_id: Optional[int] = None
   ```
   - **`target == "issue"`:**
     - `intent == "approve"` $\rightarrow$ transitions to `in_dev`, removes `flow:needs-approval` / `flow:plan` / `flow:escalated`, sets `flow:in-progress`, emits `DispatchAction(role="developer", head_sha=s.head_sha)`.
     - `intent == "plan"` (default on Issue) $\rightarrow$ transitions to `in_plan`, removes `flow:needs-approval` / `flow:escalated`, sets `flow:in-progress`, emits `DispatchAction(role="planner", head_sha=s.head_sha)`.
   - **`target == "pr"`:**
     - `intent == "evaluate"` $\rightarrow$ transitions to `in_eval`, removes `flow:escalated`, sets `flow:in-review`, emits `DispatchAction(role="evaluator", head_sha=event.head_sha or s.head_sha)`.
     - `intent == "correct"` (default on PR) $\rightarrow$ transitions to `in_dev`, removes `flow:in-review` / `flow:escalated`, sets `flow:in-progress`, emits `DispatchAction(role="developer", head_sha=event.head_sha or s.head_sha)`.
2. **Intent Parser & Trust Verification in `conductor_io.py`:**
   - Verify commenter is not a bot (`not sender.endswith("[bot]")` and `sender not in bot_logins.values()`) and is trusted (`author_association in {"OWNER", "MEMBER", "COLLABORATOR"}` or `sender == resolve_human_assignee(tenant.manifest, repo)`).
   - Parse intent after stripping `@atobar-flow-agent` / `@atobar` / `/flow` prefix:
     - On Issue: matches `^(approve|approved|lgtm|implement|start|code)\b` $\rightarrow$ `"approve"`, else `"plan"`.
     - On PR: matches `^(eval|evaluate|review|re-evaluate|re-eval|verify|check)\b` $\rightarrow$ `"evaluate"`, else `"correct"`.
   - If `state.status in ("escalated", "converged")`, mint a server-authenticated `role="epoch"` credential and post `<!-- flow:loop-epoch -->` on the Issue before applying actions so the new turn starts with a clean epoch budget.
   - Add a `👀` reaction (`POST /repos/{repo}/issues/comments/{comment_id}/reactions` with `{"content": "eyes"}`) on the user's triggering comment for immediate UX feedback.
3. **Fix `_run_serverless_arbitrator()` Unclaimed Envelope Bug (`conductor_io.py:429-455`):**
   - Capture the `comment_id` returned when `run_arbitrator()` posts the `flow-arbitration` comment and immediately call `store.claim_run_credential_envelope(run_id, comment_id)` so subsequent webhooks never ignore the arbitration envelope or falsely trip `max_pr_wall_clock_minutes`.

### WP-3: Dual-Thread Human Comments (`Issue` + `PR`) & Actionable Evaluator Verdict UX
**Files:** `flow-agent/src/flow_agent/main.py`, `flow-agent/src/flow_agent/roles/evaluator/spec.py`, `flow-agent/src/flow_agent/roles/evaluator/runner.py`, `flow-agent/src/flow_agent/roles/planner/runner.py`, `flow-agent/src/flow_agent/roles/planner/spec.py`

1. **Expand `_human_comments_for()` (`flow-agent/src/flow_agent/main.py:125-184`):**
   - Fetch trusted human comments from **both** the delegated Issue (`#53`) and any linked open PR (`#54`, including top-level issue comments on the PR, PR review bodies from `/pulls/{pr}/reviews`, and inline review comments from `/pulls/{pr}/comments` formatted as `[PR #54 file:line] @user: ...`).
   - Pass `human_comments` into `run_planner()`, `run_developer()`, and `run_evaluator()` (`build_evaluator_context()`).
2. **Always Post Full Evaluator Verdict + "Your Next Action" on the Open PR (`flow-agent/src/flow_agent/roles/evaluator/spec.py`):**
   - Remove the `if md_lines:` gate in `post_evaluator_verdict()` (`spec.py:258`) so the Evaluator **always** posts its review summary on the open PR (`#54`) as well as the Issue (`#53`).
   - Append a deterministic `format_evaluator_next_action(verdict, max_review_rounds)` block to the Evaluator comment covering:
     - `verdict == "approved"` (how to merge after `flow/arbiter`, how to request code changes via `@atobar-flow-agent <correction>`, or how to request re-evaluation via `@atobar-flow-agent evaluate <check>`),
     - `verdict == "changes_requested"` with remaining rounds (auto-rework notice + how to steer via `@atobar-flow-agent`), and
     - `verdict == "changes_requested"` at round cap (how to resume via `@atobar-flow-agent <clarification>` or `flow:resume`).

### WP-4: Verifiable Screenshot Provenance Metadata in Supabase & PR Fold-Up Summary
**Files:** `flow-agent/src/flow_agent/tools/vision_tools.py`, `control-panel/src/control_panel/app.py`, `flow-agent/src/flow_agent/roles/developer/runner.py`, `flow-agent/src/flow_agent/roles/evaluator/spec.py`

1. **Extend `inspect_ui()`, `capture_evaluator_screenshot()`, and `record_visual_artifact()` (`vision_tools.py`):**
   - Add parameters:
     - `data_mode: Literal["live", "synthetic", "hybrid"] = "live"`
     - `data_source: str = ""`
     - `constraints: str = ""`
     - `scenario: str = ""`
   - Collect host OS telemetry at capture time via helper `_collect_display_provenance(display=":0")`:
     - Run `xwininfo -root -children` on `DISPLAY=:0` (timeout 2s) to extract mapped client windows (`x11_windows: list[str]`, `has_active_window: bool`).
     - Run `ps -eo pid,etimes,args` (timeout 2s) filtered to non-system user processes in the worktree / X11 session (`active_processes: list[str]`).
     - Extract PNG image dimensions (`resolution: "<w>x<h>"`) and `captured_at` UTC ISO-8601 timestamp.
2. **Persist Provenance in Control Panel & Supabase `run_events` (`control-panel/src/control_panel/app.py:780-865`):**
   - Extend `RunScreenshotUploadPayload` with optional `metadata: Optional[dict[str, Any]] = None` (backward-compatible).
   - Merge validated metadata (`data_mode`, `data_source`, `constraints`, `scenario`, `resolution`, `x11_windows`, `active_processes`, `has_active_window`, `captured_at`) into `evt_payload` posted to `fe.post_run_events(run_id, {"events": [{"seq": ..., "kind": "screenshot", "payload": evt_payload}]})`.
3. **Render Human-Readable `<details>` Fold-Up Summary Under Every Screenshot (`vision_tools.py`):**
   - Add `render_screenshot_with_provenance(entry: dict[str, Any]) -> str` and use it in `developer/runner.py` (`_publish_ack`), `evaluator/spec.py` (`post_evaluator_verdict`), and `telemetry/github_ui.py`:
     ```markdown
     ![UI Inspection Screenshot](https://cp.example.invalid/v1/runs/run_.../screenshots/....png?exp=...&sig=...)
     <details>
     <summary>🔍 <b>Verification Provenance</b> — Mode: <code>🧪 SYNTHETIC DATA</code> · Window: <code>xterm (160x45)</code> · SHA-256: <code>d4f5205c2ced</code></summary>

     | Field | Value |
     | :--- | :--- |
     | **Data Mode** | `synthetic` *(or `live` / `hybrid`)* |
     | **Data Source** | `/tmp/demo_monitor.csv (120 rows: idle -> burst -> spike -> cooldown)` |
     | **Constraints / Conditions** | `t4g.large Spot VM (ARM64 CPU-only, 8GB RAM, no GPU or local Ollama daemon)` |
     | **Scenario Tested** | `Braille 2x upsampled 120-pt chart with per-chart refresh flags` |
     | **X11 Window (`DISPLAY=:0`)** | `xterm (1600x900+0+0)` |
     | **Active Command** | `.venv/bin/python src/monitor_tui.py --csv /tmp/demo_monitor.csv ...` |
     | **Artifact** | `run_79380f3368d6c33d` · `sha256:d4f5205c2ced202a...` (`1920x1080`, `48,995 bytes`) |

     </details>
     ```

### WP-5: Fix `artifacts_dir` Resolution Bug, Blank X11 Capture Filter & `xterm` AMI/Boot Install
**Files:** `flow-agent/src/flow_agent/tools/vision_tools.py`, `infra/packer/scripts/install_deps.sh`, `infra/docker/Dockerfile.test`, `infra/terraform/control-panel/scripts/cloud_init.sh.tpl`, `contracts/src/atobar_contracts/github_rest.py`

1. **Fix `resolve_default_artifacts_dir()` and `screenshots.json` Mirroring (`vision_tools.py:20-25, 130-165`):**
   - Resolve the default artifacts directory via `workspace_dir()` from `flow_agent.tools.git_tools` (which checks `FLOW_WORKSPACE_DIR` $\rightarrow$ `WORKSPACE_DIR` $\rightarrow$ CWD).
   - Always mirror `screenshots.json` entries to `Path(workspace_dir()) / ".atobar/artifacts/screenshots.json"` even when an explicit `artifacts_dir` is passed.
   - Remove `artifacts_dir` from the public MCP tool signature of `inspect_ui` and `capture_evaluator_screenshot` (or ignore LLM-supplied paths outside `workspace_dir()`).
2. **Filter Blank Desktop Captures (`vision_tools.py:62-91`):**
   - If `has_active_window` is `False` at capture time, include `"warning": "No active X11 application window detected on DISPLAY=:0 — verify your background UI process is still running"` in the tool return dict.
   - In `load_recorded_screenshots()`, if a run contains both `has_active_window == False` and `has_active_window == True` entries, filter out the `has_active_window == False` entries so failed launch attempts never appear in PR comments.
3. **Guarantee `xterm` and `x11-utils` (`xwininfo`) on Spot VMs:**
   - Add `x11-utils` alongside `xterm` in `infra/packer/scripts/install_deps.sh:60` and `infra/docker/Dockerfile.test`.
   - Add an idempotent Phase-0 check in `infra/terraform/control-panel/scripts/cloud_init.sh.tpl` (and `launch_spot_instance` UserData in `contracts/src/atobar_contracts/github_rest.py`):
     ```bash
     if ! command -v xterm >/dev/null 2>&1 || ! command -v xwininfo >/dev/null 2>&1; then
         DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq xterm x11-utils || true
     fi
     ```

### WP-6: Developer & Evaluator Prompt Steering (`developer/prompt.py`, `evaluator/prompt.py`, `SOUL.md`)
**Files:** `flow-agent/src/flow_agent/roles/developer/prompt.py`, `flow-agent/src/flow_agent/roles/evaluator/prompt.py`, `SOUL.md`

1. **Developer Prompt Additions (`developer/prompt.py`):**
   - Require **Live-First Verification**: run the repository's real data collector/producer (`data_mode="live"`) whenever supported by the Linux CPU sandbox.
   - When hardware/OS/service constraints require synthetic fixtures (`data_mode="synthetic"`), **forbid uniform `math.sin` / `math.cos` waves**. Require multi-regime traces (idle/zero baseline, step-change burst, noisy jitter, saturation spike, and cooldown) and truthful `data_mode`, `data_source`, `constraints`, and `scenario` arguments to `inspect_ui()`.
2. **Evaluator Prompt Additions (`evaluator/prompt.py` & `SOUL.md`):**
   - **Strict Fixture Independence (Anti-Anchoring):** Forbid copying or reusing the Developer's synthetic fixture script, formula, or model names from the PR diff/description.
   - Require the Evaluator to test **adversarial boundary cases** (zeros, maximums, malformed/missing rows, `None` model state, and window resize/flag combinations) and verify that the Developer's screenshot provenance accurately declares any synthetic data constraints.

---

## Part 4 — Adversarial Corner-Cases Audit

| Implementation Detail | Concurrency / Race | Partial Failure / Retry Idempotency | Pagination / Bounds / Truncation | Auth / Trust Boundary |
| :--- | :--- | :--- | :--- | :--- |
| **1. `@atobar-flow-agent` Comment Trigger (WP-1/2)** | Two comments posted rapidly on PR #54 $\rightarrow$ serialized by `_lock_for_issue(tenant.id, issue_num)`; if a session is already active, record the comment for the next turn without spawning a duplicate Spot VM. | Re-delivered GitHub webhook $\rightarrow$ deduplicated by `comment:{issue_num}:{comment_id}` in `processed_events`. | Comment body clipped to `_MAX_COMMENT_CHARS = 2000` (total `8000` chars across epoch) in `_human_comments_for()`. | Bot logins (`*[bot]`, `get_bot_logins()`) are strictly ignored so Evaluator's own help footer (`@atobar-flow-agent ...`) never self-triggers an infinite loop. Only `OWNER`, `MEMBER`, `COLLABORATOR`, or `owner_github_login` can trigger dispatches. |
| **2. PR-to-Issue Resolution (WP-1)** | PR comment arrives before `loop_rounds` has a row $\rightarrow$ resolved deterministically from PR branch (`flow/issue-<N>`) or `Fixes #<N>` in PR body via GitHub API. | If PR lookup fails or PR is not a tracked factory PR $\rightarrow$ returns `status="ignored"` cleanly without mutating state. | `get_comments` / `get_pr` paginated up to 10 pages (1,000 comments). | Human comments from PR reviews/threads are wrapped in `wrap_untrusted(..., source="human_comment")` so prompt-injection delimiters are neutralized. |
| **3. Serverless Arbitrator Claim (WP-2)** | Webhook for `flow-arbitration` arrives 5–20s after `_run_serverless_arbitrator` finishes $\rightarrow$ `envelope_comment_id` is already claimed on `run_id`, so `rebuild_loop_state` immediately recognizes `latest_arbitration` and `status="converged"`. | Claiming the same `(run_id, comment_id)` twice is idempotent in `store.claim_run_credential_envelope()`. | — | Only the server-minted `role="arbitrator"` `run_id` can claim the arbitration comment. |
| **4. OS Provenance & Screenshot Upload (WP-4/5)** | `xwininfo` or `ps` subprocess hangs $\rightarrow$ bounded by `timeout=2.0` seconds with `try/except` fallback so screenshot capture never crashes. | Older agents or unit tests calling `POST /v1/runs/{run_id}/screenshots` without `metadata` $\rightarrow$ defaults to empty provenance dict (`201 Created`). | `x11_windows` and `active_processes` lists capped at 10 entries / 500 chars each; PNG size capped at 10 MB. | `x11_windows`, `active_processes`, `resolution`, and `sha256` are collected by deterministic Python code in `vision_tools.py`, not free-form LLM markdown. |

---

## Part 5 — Success Criteria & Acceptance Verification Plan

1. **Automated Verification Gate (`uv run --no-sync atobar-ops verify`):**
   - `ruff`, combined `mypy`, root `pytest -q`, `atobar_contracts.schema --check`, `atobar_contracts.manifest`, and `gitleaks` all pass with zero warnings/errors.
2. **Unit & Integration Test Expectations:**
   - `contracts/tests/test_machine.py` & `test_conductor_io.py`:
     - Commenting `@atobar-flow-agent <plan adjustment>` on Issue `#53` dispatches `planner`; commenting `@atobar-flow-agent approved` dispatches `developer`.
     - Commenting `@atobar-flow-agent <fix request>` on PR `#54` resolves to Issue `#53` and dispatches `developer` on PR `#54`'s `head_sha`; commenting `@atobar-flow-agent evaluate <check>` on PR `#54` dispatches `evaluator`.
     - Commenting on an `escalated` or `converged` PR automatically posts a signed `<!-- flow:loop-epoch -->` marker, clears `flow:escalated`, and dispatches the requested role.
     - Bot-authored comments containing `@atobar-flow-agent` (such as Evaluator's footer) and untrusted `NONE` association comments are rejected (`status="ignored"`).
     - `_run_serverless_arbitrator()` claims `envelope_comment_id` so subsequent webhooks on `#53` preserve `status="converged"` and never trip `max_pr_wall_clock_minutes`.
   - `flow-agent/tests/roles/test_evaluator_verdict_ux.py`:
     - `post_evaluator_verdict()` always posts the review summary + state-aware `### 👉 Your Next Action` footer on the open PR even when 0 screenshots were recorded.
     - `_human_comments_for()` collects trusted comments from both Issue `#53` and PR `#54` and injects them into `planner`, `developer`, and `evaluator` contexts.
   - `flow-agent/tests/tools/test_vision_tools.py` & `control-panel/tests/test_screenshots_endpoint.py`:
     - `capture_evaluator_screenshot()` and `inspect_ui()` resolve `FLOW_WORKSPACE_DIR` via `workspace_dir()`, mirror `screenshots.json` regardless of `artifacts_dir`, and filter out `has_active_window=False` blank captures when a valid capture exists.
     - `POST /v1/runs/{run_id}/screenshots` stores `data_mode`, `data_source`, `constraints`, `scenario`, `x11_windows`, and `active_processes` in the Supabase `run_events` (`kind="screenshot"`) payload, and renders the `<details><summary>🔍 Verification Provenance...</summary>...</details>` fold-up block below each screenshot.
3. **Live E2E Acceptance Check on `arsenyspb/ollama-monitor`:**
   - Posting `@atobar-flow-agent evaluate please verify the TUI with an idle 0-TPS period and step burst` in the comment box of `arsenyspb/ollama-monitor/pull/54` adds a `👀` reaction, resets the escalated epoch on `#53`, dispatches the `evaluator` on `9261fc0`, and posts a new PR review comment containing:
     1. A screenshot captured via `xterm` showing non-sinewave multi-regime data,
     2. The collapsible `🔍 Verification Provenance` `<details>` section (matching the `kind="screenshot"` entry in Supabase `run_events`) explicitly listing `data_mode: synthetic` and the Spot VM hardware constraints, and
     3. The `### 👉 Your Next Action` invitation block at the bottom of the Evaluator's comment.
