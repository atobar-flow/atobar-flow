# J38 (planner-fast-track-and-spot-reuse): Spec-Sufficiency Triage Gate, Separated PLANNER Role (`flow-plan`), and Warm Spot Instance Reuse via Isolated Git Worktrees

- **GitHub Issue:** [#311](https://github.com/atobar-flow/atobar-flow/issues/311)
- **Priority:** P0 (Core Swarm Latency, Token Efficiency & AI-DLC Architecture Evolution)
- **Status:** Open / Proposed
- **Milestone:** M05 / M09 (Swarm Roles, Goal/Plan Decomposition & Runtime Harness Engineering)
- **Blocked by:** none
- **Blocks:** M05feat07 (`#203`), M05feat08 (`#204`), M06feat04 (`#138`)
- **Owner:** factory + human
- **Owner Directives:**
  1. Leverage `atobar-flow`'s structured onboarding artifacts (`TenantManifest`, `SotaWiki` in `public.repository_wikis`, and canonical issue templates) to separate **PLANNER** as a first-class swarm agent (`src/flow_engine/roles/planner/`).
  2. Introduce a deterministic **Spec-Sufficiency Gate** at `flow:*` ingress that judges whether an issue is already concrete enough to fast-track straight to the **DEVELOPER (Coder)** vs. requiring **PLANNER** decomposition (`flow-plan` Task Ledger) first.
  3. Re-use the provisioned EC2 Spot instance across the entire per-issue/PR role lifecycle (`PLANNER` → `DEVELOPER` → `EVALUATOR` → `DEVELOPER` rework → `ARBITRATOR`) via isolated `git worktree` sandboxes and a Control Panel lease-handover protocol—eliminating redundant Spot provisioning, repo cloning, and setup overhead while strictly preserving SOTA'26 Axiom 3, Axiom 8, Axiom 11, and Axiom 12.

---

## Read First
```bash
rg -n "Axiom (4|5|6|8|10|11|12)" docs/SOTA26_HEURISTICS.md
rg -n "Trajectory Phase|Dual-Ledger|AgentCoder|worktrunk" docs/SOTA26_AGENT_PROMPTS.md
rg -n "Role Boundaries|Async PR-Comment Handoff|Contributing to the factory" docs/AGENTS.md
```

---

## Part 1 — Executive Assessment: Current Handover Bottlenecks vs. SOTA'26 Harness Engineering

| ID | Layer & Exact Code Location | Current Behaviour & Failure Mode | SOTA'26 Reference (`SOTA26_HEURISTICS.md` & `SOTA26_AGENT_PROMPTS.md`) | Impact |
| :--- | :--- | :--- | :--- | :--- |
| **GAP-01** | **Unconditional Developer Dispatch on `flow:*` Ingress**<br>`src/control_panel/app.py:182-277` (`ingest_event`) | Every `flow:feature`, `flow:fix`, or `flow:docs` label unconditionally mints a `developer` credential and dispatches `role="developer"`, regardless of whether the issue is a concrete, file-scoped spec (e.g. `.github/ISSUE_TEMPLATE/roadmap-task.md`) or a 1-sentence underspecified request (`"Fix dashboard overflow"`). | **SOTA'26 Prompt Architecture §1.B (Magentic Dual-Ledger) & §Trajectory Phase 1 & 3:** Trajectory convergence depends on specification specificity. Underspecified work items require Outer-Loop Task Ledger planning + Agentless localization before Inner-Loop coding; fully specified items should bypass multi-agent planning overhead. | Underspecified issues burn 25–50 Developer turns doing unguided repo exploration; well-specified issues have no formal `flow-plan` contract when needed. |
| **GAP-02** | **Orphaned Onboarding Wiki (`SotaWiki` Never Injected at Runtime)**<br>`src/flow_engine/main.py:201-212`, `src/flow_engine/roles/developer/spec.py:38` | Onboarding (`src/flow_engine/tenancy/onboard.py:66`, `src/flow_engine/knowledge/onboard_repo.py:113`) synthesizes a structured `SotaWiki` (`purpose`, `technologies`, `structure`, `concepts`) into `public.repository_wikis`, and `GET /v1/wiki` (`src/control_panel/app.py:124`) serves it. However, `main.py` calls `run_developer(..., task_prompt=issue_text)` **without fetching or passing `wiki`**! | **Axiom 4 (Context Ingestion / Anti-RAG):** *"Inject deterministic, schema-validated architectural summaries (`repository_wikis`) directly into the static prompt prefix."* | Both Planner and Developer start completely blind to the repository's pre-computed architecture and directory structure on every run. |
| **GAP-03** | **Single-Role Spot VM Termination (`sudo shutdown -h now`)**<br>`src/flow_engine/main.py:69-72`, `src/flow_engine/conductor/io.py:313-353`, `.github/workflows/flow_dispatcher.yml` | Every role transition (`developer` → `evaluator` → `developer` R2 → `evaluator` R2 → `arbitrator`) spawns a brand-new GitHub Actions job (`flow_dispatcher.yml`) + a new `aws ec2 run-instances` Spot VM (`t4g.large`), runs `cloud_init.sh.tpl` (`git clone` × 2, `uv sync --extra agents`, `.atobar/setup.sh`), executes **one** role, and executes `sudo shutdown -h now` (terminating the instance). | **Axiom 5 (Phase 0 Deterministic Scaffolding) & SOTA'26 §Trajectory Phase 2 (`worktrunk` Git Worktrees):** Production 2026 harnesses reuse a warm sandbox container/VM across delegated sub-agents via isolated `git worktree` checkouts and isolated `EventLog`/session directories. | A 2-round PR provisions **5 EC2 Spot VMs** and wastes **10–17 minutes** in cold boot, clone, and `uv sync` latency. |
| **GAP-04** | **Duplicate Phase 0 Setup Execution on Every Boot**<br>`infra/terraform/control-panel/scripts/cloud_init.sh.tpl:132-135` & `src/flow_engine/main.py:165` (`sandbox.prepare`) | `cloud_init.sh.tpl` clones `TARGET_REPO` into `/home/ubuntu/workspace` and runs `./.atobar/setup.sh`. Seconds later, `main.py` invokes `sandbox.prepare()`, which runs `manifest.metrics_runtime.setup` again in `/home/ubuntu/workspace`. | **Axiom 5 (Phase 0 Deterministic Scaffolding):** *"AI reasoning tokens and wall-clock budget are expensive. Do not spend them on redundant environment setup."* | Doubles setup latency and risks non-idempotent setup command failures on role startup. |
| **GAP-05** | **Arbitrator Provisions a Full X11 Spot VM for 3 HTTP Calls**<br>`src/flow_engine/main.py:226-346`, `src/flow_engine/roles/arbitrator/run.py` | When `machine.step()` reaches `status == "converged"`, `apply_actions()` dispatches `role="arbitrator"` via `flow_dispatcher.yml`, booting an EC2 Spot VM with X11/Openbox/Chromium just to fetch `/arbitration-data`, run pure function `evaluate_gate()`, and post `flow-arbitration`. | **Axiom 13 (Deterministic Business Arbitration):** *"The pass/fail decision is a pure function `arbitrate(policy, observations) -> Decision`, unit-tested and with no LLM."* | Wastes ~3 minutes and Spot capacity for a pure deterministic check that requires zero sandbox code execution. |

---

## Part 2 — `<self_doubt>`: Arguing With Ourselves Over Every Step of the Re-Architecture

<self_doubt>

### Q1: Does judging whether an issue can skip the PLANNER violate the factory rule: *"Deterministic components (`conductor/`, `protocol/`, `roles/arbitrator/gate`, `tenancy/`) MUST NOT import an LLM client"* (`docs/AGENTS.md:64`)?
**Argument against putting an LLM router inside `conductor/`:**
Yes! If `src/flow_engine/conductor/machine.py` or `src/flow_engine/conductor/io.py` imported an LLM client to classify issue specificity, it would violate `docs/AGENTS.md` ("Deterministic components (`conductor/`, `protocol/`, `roles/arbitrator/gate`, `tenancy/`) MUST NOT import an LLM client") and **Axiom 12** ("The CONDUCTOR is a deterministic, event-driven state machine (no LLM)").
**Resolution:**
Split the Spec-Sufficiency Gate into two strictly separated layers:
1. **Deterministic Structural Gate (`src/flow_engine/conductor/sufficiency.py` — pure Python, zero LLM imports):**
   - Inspects the issue markdown and labels using deterministic parsing (reusing section extraction patterns from `src/flow_engine/tenancy/spec.py`):
     - **`route = "developer"` (Fast-Track Straight to Coder)** when:
       - Label is `flow:feature`, `flow:fix`, or `flow:docs` (not `flow:plan`), **AND**
       - Issue body contains explicit **Scope / Target Path markers** (e.g., `## Scope` section or explicit backticked repo file paths like `` `src/...` `` / `` `tests/...` `` or function/CLI flags with >150 chars of structured specification) **AND**
       - Issue body contains explicit **Acceptance Criteria / Verification markers** (e.g., `## Acceptance criteria`, `- [ ]`, `AC-1`, `## Verify`, or `## Steps to reproduce` + `## Expected`).
     - **`route = "planner"` (Requires Planner Agent first)** when:
       - Label is `flow:plan`, **OR**
       - Issue body lacks target scope/file anchors or lacks acceptance criteria / reproduction steps (e.g., vague 1-paragraph feature request or high-level objective).
2. **In-Sandbox Fast-Exit inside `run_planner()` (if `planner` was dispatched on a borderline prose issue):**
   - Even when `conductor` deterministically routes a prose issue to `planner`, `run_planner()` runs **on the warm Spot instance** that `developer` will immediately reuse 15 seconds later!
   - Using `SotaWiki` + `search_codebase` + `read_file`, `run_planner()` localizes the target files and emits a `flow-plan` envelope in 1–3 turns, then **immediately hands over in-process/in-VM to `developer`** without provisioning a new Spot instance.

### Q2: If `PLANNER`, `DEVELOPER`, and `EVALUATOR` run on the **same** EC2 Spot instance, how do we prevent breaking **Axiom 11 (Evaluator-Optimizer Separation)** and **CSPM-02 (Credential / Transcript State-Bleed)**?
**Argument against naive single-directory reuse (`/home/ubuntu/workspace`):**
If `developer` and `evaluator` run sequentially in `/home/ubuntu/workspace`:
1. `nanobot` writes `SOUL.md`, `.nanobot/tool-results/`, `memory/MEMORY.md`, `memory/HISTORY.md`, `memory/history.jsonl`, and `.atobar/research/` directly into `workspace_dir` (`src/flow_engine/agents/nanobot_config.py:11-22`).
2. If `evaluator` runs in `/home/ubuntu/workspace`, it will inherit `developer`'s `memory/history.jsonl` and `.atobar/research/` notes—violating **Axiom 11**: *"An agent that verifies its own work suffers confirmation bias. Shared memory between the author and the checker turns the Evaluator-Optimizer pattern into self-grading. DEV transcripts are never shared."*
3. Furthermore, uncommitted files or dirty build artifacts left behind by `developer` could cause tests to pass locally in `/home/ubuntu/workspace` even though those files were never committed to `head_sha`!
**Resolution (Physical Worktree + Per-Run Directory + Credential Isolation on One Spot VM):**
1. **Immutable Base Clone + Ephemeral Per-Role Git Worktrees (`src/flow_engine/runtime/sandbox.py`):**
   - `/home/ubuntu/repo-base`: Bare/clean mirror clone updated via `git fetch origin`.
   - Every role execution (`run_id`) gets its own isolated directory via `git worktree add --detach /home/ubuntu/worktrees/<role>-<run_id[:8]> <ref>`:
     - `planner`: `/home/ubuntu/worktrees/planner-<run_id[:8]>` (checked out at `default_branch`)
     - `developer`: `/home/ubuntu/worktrees/developer-<issue_id>` (persistent across dev rework rounds on branch `flow/issue-<issue_id>`, so round 2 resumes cleanly without re-cloning!)
     - `evaluator`: `/home/ubuntu/worktrees/evaluator-<head_sha[:8]>-<run_id[:8]>` (fresh detached worktree at exact pushed `head_sha` fetched from `origin`, with zero untracked files and zero access to `developer`'s worktree or `/home/ubuntu/flow-run/<dev_run_id>`).
2. **Strict `nanobot` Workspace Containment (`restrictToWorkspace: True`):**
   - `render_nanobot_run_config()` (`src/flow_engine/agents/nanobot_config.py:87`) sets `agent_ws = Path(workspace_dir)` (the role's isolated worktree) and `config_path = /home/ubuntu/flow-run/<run_id>/config.json`.
   - Because `restrictToWorkspace: True` is enforced by `nanobot` and `WORKSPACE_DIR` is passed to `flow_engine.tools_mcp.server`, `evaluator` is jailed inside `/home/ubuntu/worktrees/evaluator-<head_sha[:8]>-<run_id[:8]>` and cannot read `/home/ubuntu/worktrees/developer-*` or `/home/ubuntu/flow-run/<dev_run_id>`.
3. **Per-Role Single-Use Credentials & Separate Model Configs (`tenant_role_llm`):**
   - The Spot Supervisor **never** reuses `FLOW_RUN_ID` or `FLOW_RUN_CREDENTIAL` across roles.
   - Before starting each role in the warm loop, the Supervisor receives a fresh role-scoped `(run_id, run_credential, role, head_sha)` from the Control Panel, calls `fetch_run_llm_key(cp_url, run_id, run_cred)`, and configures `nanobot` with that role's dedicated provider/model/reasoning_effort (e.g. `claude` for Evaluator vs `gemini` for Developer).

### Q3: How does the Conductor (`machine.py` / `io.py`) coordinate with a warm Spot instance without creating a race condition where **both** the warm Spot instance AND `flow_dispatcher.yml` run the next role?
**Argument against asynchronous webhook races:**
Today, when `developer` posts a `flow-ack` comment on GitHub:
1. GitHub fires `issue_comment.created` to the Client Repo's `flow-trigger.yml` Action (~5–15s delay).
2. `flow-trigger.yml` calls `POST /v1/events` on the Control Panel.
3. Control Panel emits `repository_dispatch("flow_event")` to `atobar-flow`'s `flow_conductor.yml` Action (~10–20s delay).
4. `flow_conductor.yml` runs `process_flow_event()` → `apply_actions()` → emits `repository_dispatch("flow_run")` to `flow_dispatcher.yml`, which calls `aws ec2 run-instances`.
If the warm Spot instance *also* transitioned to `evaluator` immediately, then 30 seconds later `flow_conductor.yml` would process the `issue_comment.created` webhook and either spawn a duplicate Spot VM or collide!
**Resolution (Synchronous Control Plane Turn Handover + Event Idempotency):**
1. **New Control Panel Endpoint `POST /v1/runs/{run_id}/turn-complete`:**
   - Right after a role runner (`run_planner`, `run_developer`, `run_evaluator`) posts its `flow-*` envelope comment to GitHub (returning the created GitHub `comment_id` and `comment_body`), the Spot Supervisor calls `POST /v1/runs/{run_id}/turn-complete` on the Control Panel with `{comment_id, comment_body, head_sha, usage}`.
   - The Control Panel **immediately** invokes `process_flow_event()` in-process using the deterministic `compute_event_id()` (`kind="issue_comment.created"`, `comment_id`, `issue_number`).
   - Because `process_flow_event()` records `event_id` in `processed_events` (`store.record_processed_event(tenant.id, event_id)`), when the delayed GitHub webhook from `flow-trigger.yml` arrives 15 seconds later with the same `comment_id`, `store.has_processed_event(tenant.id, event_id)` returns `True` and **idempotently skips it (`status: "skipped", reason: "duplicate event"`)**!
2. **Warm Lease Claim vs. Cold Spot Fallback in `apply_actions()`:**
   - When `process_flow_event()` executes `apply_actions()` for a `DispatchAction(role=next_role, head_sha=...)`:
     - It mints `(next_run_id, next_run_cred)` in `run_credentials`.
     - If `apply_actions()` was invoked synchronously via `POST /v1/runs/{run_id}/turn-complete` from a live Spot instance (`reuse_instance_id`), it binds `sessions` (`instance_id = reuse_instance_id`, `run_id = next_run_id`, `role = next_role`, `status = "Running"`) and returns `{action: "run_role", role: next_role, run_id: next_run_id, run_credential: next_run_cred, head_sha: ...}` directly in the HTTP response to the Spot Supervisor—**skipping `gh.dispatch_flow_run()` entirely**!
     - If `apply_actions()` was invoked asynchronously (e.g., human added `flow:resume` or external CI `check_run.completed` arrived after the Spot VM shut down) and no warm Spot instance is waiting, it dispatches `flow_run` (or runs `arbitrator` in-process) as before.

### Q4: What happens if a PR has `required_checks` in `TenantManifest` that take 10 minutes in GitHub Actions, or if `arbitrator` is the only remaining step?
**Argument against keeping a Spot VM idle indefinitely:**
- In `machine.py:118-137`, `_check_convergence(state)` requires **both** `verdict == "approved"` AND `all(state.required_checks_status.get(c) == "success" for c in state.required_checks)`.
- Notice that in `machine.py:219`, `DispatchAction(role="evaluator")` is triggered immediately upon `AckEvent` (it does **not** wait for `required_checks` to finish before starting `evaluator`).
- Once `evaluator` emits `verdict == "approved"`:
  - If `required_checks` are already green (or empty, as in `tenants/arsenyspb__ollama-monitor.json`), `machine.step()` immediately emits `DispatchAction(role="arbitrator", head_sha=s.head_sha)`! The warm Spot instance (or CP in-process) runs `arbitrator` in <2 seconds and then shuts down cleanly.
  - If external `required_checks` are still `pending` after `evaluator` approves, no more Spot VM compute (X11/filesystem/LLM) is needed unless a check fails or `arbitrator` runs.
  - Therefore, once `evaluator` emits `approved` and `status` is waiting only on external `check_run.completed`, the Spot Supervisor can **shut down immediately** (`action: "shutdown"`), and when the final `check_run.completed` event arrives at the Control Panel, the Conductor executes `run_arbitrator()` **directly in-process (serverless, zero Spot VM)**!

### Q5: How does `flow_gc.py` (`evaluate_gc_candidates`) behave when a single Spot instance runs multiple roles across rounds?
**Argument from `src/flow_engine/conductor/gc.py:42-67`:**
`evaluate_gc_candidates()` terminates any Spot instance where `age_minutes >= max_wall_clock_minutes` (default `60.0` in `gc.py`, or `manifest.loop.max_pr_wall_clock_minutes` in `machine.py`).
- Previously, each role booted a new instance (resetting `LaunchTime`), whereas `machine.py` tracked `accumulated_wall_clock_minutes` across rounds against `max_pr_wall_clock_minutes`.
- With Spot reuse, a single instance's `age_minutes` naturally equals the PR's `accumulated_wall_clock_minutes`!
- We must also update the EC2 `Role` tag (or `sessions.role` in Supabase) on role transitions so `atobar-ops watch` and `flow_gc` accurately reflect the currently executing role (`planner` | `developer` | `evaluator` | `arbitrator`).

</self_doubt>

---

## Part 3 — Target Architecture & Stigmergic Protocol

### 1. End-to-End Warm Handover Topology

```
Client Repo: Issue labeled `flow:feature` | `flow:fix` | `flow:docs` | `flow:plan`
  │
  ▼
Control Panel (`POST /v1/events` -> `src/flow_engine/conductor/sufficiency.py`)
  │  Deterministic Spec-Sufficiency Gate (`assess_issue_sufficiency(issue_text, label)`):
  │   ├─ Fast-Track (`initial_role = "developer"`): Issue has Scope/File anchors + Acceptance Criteria
  │   └─ Plan-First (`initial_role = "planner"`):   Label is `flow:plan` OR issue is underspecified
  │
  │  Spawns ONE EC2 Spot VM (`flow_dispatcher.yml`) with `FLOW_ROLE = initial_role`
  ▼
Warm Spot Instance Supervisor (`src/flow_engine/main.py` — 1 VM for entire Issue/PR Epoch)
  │
  ├─ Phase 0 (Runs ONCE per VM boot):
  │   1. Clone `TARGET_REPO` into `/home/ubuntu/repo-base` + run `.atobar/setup.sh` once
  │   2. Fetch `SotaWiki` (`GET /v1/runs/{run_id}/wiki`) + `TenantManifest` once
  │
  ├─ [If `FLOW_ROLE == "planner"`] ──▶ Worktree `/home/ubuntu/worktrees/planner-<run_id[:8]>`
  │   • Runs `run_planner()` (read-only allowlist: `read_file`, `search_codebase`, `todo_write`)
  │   • Posts ```flow-plan``` envelope comment on Issue #N
  │   • Calls `POST /v1/runs/{run_id}/turn-complete`
  │   • If `complexity == "multi_issue_epic"` or `label == "flow:plan"`:
  │       CP sets `flow:needs-approval`, returns `{"action": "shutdown"}` -> VM terminates for HITL gate
  │   • If `complexity == "single_pr"`:
  │       CP transitions `in_plan -> in_dev`, mints `developer` credential, returns:
  │       `{"action": "continue", "role": "developer", "run_id": "...", "run_credential": "..."}`
  │
  ├─ [Role: `developer` (Round k)] ──▶ Worktree `/home/ubuntu/worktrees/developer-<issue_id>`
  │   • Injects `SotaWiki` + `latest_plan` (`flow-plan`) + `latest_verdict` (`flow-verdict`)
  │   • Fetches `developer` LLM key & model from `POST /v1/runs/{run_id}/llm-key`
  │   • Runs `nanobot` loop -> commits TDD tests & fix -> `create_and_verify_pr` -> posts ```flow-ack```
  │   • Calls `POST /v1/runs/{run_id}/turn-complete`
  │   • CP transitions `in_dev -> in_eval`, mints `evaluator` credential, returns:
  │       `{"action": "continue", "role": "evaluator", "run_id": "...", "head_sha": "<pushed_sha>"}`
  │
  ├─ [Role: `evaluator` (Round k)] ──▶ Fresh Worktree `/home/ubuntu/worktrees/evaluator-<sha[:8]>-<run_id[:8]>`
  │   • Detached checkout of exact `<pushed_sha>` (zero dev untracked files, zero dev memory/transcripts)
  │   • Fetches `evaluator` LLM key & model from `POST /v1/runs/{run_id}/llm-key`
  │   • Verifies ACs (from Issue + `flow-plan`) & visual X11 assertions -> posts ```flow-verdict``` + `flow/eval` check
  │   • Calls `POST /v1/runs/{run_id}/turn-complete`
  │   • CP runs `machine.step()`:
  │       ├─ If `verdict == "changes_requested"` (and under caps):
  │       │    Returns `{"action": "continue", "role": "developer", "run_id": "...", "head_sha": "<sha>"}`
  │       │    (Developer R(k+1) starts in <2 seconds in warm `/home/ubuntu/worktrees/developer-<issue_id>`!)
  │       ├─ If `verdict == "approved"` and `required_checks` green:
  │       │    Returns `{"action": "continue", "role": "arbitrator", ...}` (or CP runs Arbitrator in-process)
  │       └─ If escalated or waiting on slow external CI:
  │            Returns `{"action": "shutdown"}` -> VM runs `sudo shutdown -h now`
```

### 2. New `flow-plan` Stigmergic Envelope (`src/flow_engine/protocol/envelopes.py`)

```python
class PlannedCriterion(BaseModel, extra="forbid"):
    id: str
    description: str
    verification_cmd: str = ""


class Plan(BaseEnvelope):
    role: Literal["planner"] = "planner"
    complexity: Literal["single_pr", "multi_issue_epic"] = "single_pr"
    goal: str
    scope_files: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    acceptance_criteria: list[PlannedCriterion] = Field(default_factory=list)
    implementation_steps: list[str] = Field(default_factory=list)
```

Rendered in the issue/PR thread as:
````markdown
```flow-plan
{
  "v": 1,
  "tenant": "owner/repo",
  "pr": 42,
  "head_sha": "0000000000000000000000000000000000000000",
  "run_id": "run_...",
  "role": "planner",
  "complexity": "single_pr",
  "goal": "...",
  "scope_files": ["src/...", "tests/..."],
  "out_of_scope": ["..."],
  "acceptance_criteria": [
    {"id": "AC-1", "description": "...", "verification_cmd": "pytest tests/test_x.py -q"}
  ],
  "implementation_steps": ["..."]
}
```
````
*(Note: Because a `flow-plan` is emitted on an issue before a PR branch SHA exists, `Plan` permits a 40-hex `head_sha` including the sentinel `"0" * 40` or base branch SHA.)*

---

## Part 4 — Work Packages (`WP-1` .. `WP-6`) & TDD Execution Plan

### WP-1: Protocol & Deterministic Spec-Sufficiency Gate (`envelopes.py`, `sufficiency.py`, `machine.py`)
- **Goal:** Add the `Plan` (`flow-plan`) envelope to `src/flow_engine/protocol/envelopes.py`, implement the pure-Python `assess_issue_sufficiency()` gate in `src/flow_engine/conductor/sufficiency.py`, and extend `LoopState` / `step()` in `src/flow_engine/conductor/machine.py` with `in_plan` state and `PlanEvent` transitions.
- **Scope:**
  - `src/flow_engine/protocol/envelopes.py`, `src/flow_engine/protocol/__init__.py`
  - `src/flow_engine/conductor/sufficiency.py` (new)
  - `src/flow_engine/conductor/machine.py`
  - `tests/protocol/test_envelopes.py`, `tests/conductor/test_sufficiency.py` (new), `tests/conductor/test_machine.py`
- **Out of scope:** LLM calls (strictly forbidden in `protocol/` and `conductor/`), Control Panel HTTP wiring (WP-4).
- **Acceptance Criteria (tests first):**
  - [ ] **AC-1:** `parse()` and `render()` in `envelopes.py` round-trip `Plan` (`flow-plan`) envelopes and enforce `role == "planner"` + author binding.
  - [ ] **AC-2:** `assess_issue_sufficiency(issue_text, label)` returns `SufficiencyDecision(route="developer", reason=...)` when `issue_text` contains both Scope/file path indicators (`## Scope` or backticked file paths) and Acceptance Criteria (`## Acceptance criteria`, `- [ ]`, `AC-`, or reproduction/expected sections), and returns `route="planner"` when `label == "flow:plan"` or when scope/AC sections are missing.
  - [ ] **AC-3:** `machine.step()` handles `PlanEvent`:
    - When `plan.complexity == "single_pr"` and trigger label is not `"flow:plan"`, transitions `status` to `"in_dev"` and emits `[DispatchAction(role="developer", head_sha=state.head_sha), SetLabelAction(label="flow:in-progress")]`.
    - When `plan.complexity == "multi_issue_epic"` or trigger label is `"flow:plan"`, transitions `status` to `"idle"` and emits `[SetLabelAction(label="flow:needs-approval")]`.
  - [ ] **AC-4:** Zero LLM imports in `src/flow_engine/conductor/` and `src/flow_engine/protocol/`.

### WP-2: First-Class `PLANNER` Role & Wiki/Plan Context Injection (`roles/planner/`, `roles/developer/`, `roles/evaluator/`)
- **Goal:** Implement the `planner` role (`src/flow_engine/roles/planner/{prompt.py,spec.py,runner.py}`), register `"planner"` in `ROLE_ALLOWLISTS` (`src/flow_engine/runtime/tools.py`) and `tenant_role_llm`, and inject `SotaWiki` + `latest_plan` into `planner`, `developer`, and `evaluator` contexts.
- **Scope:**
  - `src/flow_engine/runtime/tools.py`
  - `src/flow_engine/roles/planner/__init__.py`, `prompt.py`, `spec.py`, `runner.py` (new)
  - `src/flow_engine/roles/developer/spec.py`, `src/flow_engine/roles/developer/runner.py`
  - `src/flow_engine/roles/evaluator/spec.py`, `src/flow_engine/roles/evaluator/runner.py`
  - `src/control_panel/app.py` (add `GET /v1/runs/{run_id}/wiki` authenticated by `FLOW_RUN_CREDENTIAL` and allow `"planner"` in `tenant_role_llm` / fallback to `"developer"` model preset if `"planner"` role model is not separately configured)
  - `tests/roles/test_planner.py` (new), `tests/roles/test_developer.py`, `tests/roles/test_evaluator.py`
- **Out of scope:** Spot supervisor loop (WP-5).
- **Acceptance Criteria (tests first):**
  - [ ] **AC-1:** `allowlist_for("planner")` permits strictly read-only localization/planning tools: `{"read_file", "search_codebase", "git_pr_checks", "git_ci_job_logs", "todo_write"}` (no `execute_command`, `write_file`, `multi_edit`, or `create_and_verify_pr`).
  - [ ] **AC-2:** `run_planner()` fetches the tenant's `SotaWiki`, runs the `AgentRuntime` until a `flow-plan` envelope is emitted, validates it as a `Plan` envelope, and posts it to the issue thread.
  - [ ] **AC-3:** `build_developer_context()` and `build_evaluator_context()` accept `latest_plan: Optional[Plan] = None` (and `wiki: Optional[dict[str, Any]] = None` for Developer) and render the structured `flow-plan` Task Ledger (scope files, ACs, verification commands) into the role context.
  - [ ] **AC-4:** If a tenant has not explicitly configured a separate model for `role="planner"` in `tenant_role_llm`, `POST /v1/runs/{run_id}/llm-key` falls back cleanly to the tenant's `developer` role model configuration so existing tenants work out-of-the-box without migration friction.

### WP-3: Worktree-Isolated Sandbox Preparation & Single Phase-0 Setup (`runtime/sandbox.py`, `cloud_init.sh.tpl`)
- **Goal:** Refactor `src/flow_engine/runtime/sandbox.py` so the target repository is cloned and set up **once** in a base directory, while each role turn executes inside an isolated `git worktree` (`/home/ubuntu/worktrees/<role>-...`) with zero transcript/file bleed across roles. Eliminate the duplicate `.atobar/setup.sh` execution between `cloud_init.sh.tpl` and `main.py`.
- **Scope:**
  - `src/flow_engine/runtime/sandbox.py`
  - `src/flow_engine/tools/git_tools.py` (`workspace_dir()` resolution via `WORKSPACE_DIR`)
  - `infra/terraform/control-panel/scripts/cloud_init.sh.tpl`
  - `tests/runtime/test_sandbox.py`
- **Out of scope:** Control Panel endpoints (WP-4).
- **Acceptance Criteria (tests first):**
  - [ ] **AC-1:** `prepare_role_worktree(base_repo_dir, worktrees_root, role, ref, run_id, issue_id)` creates/updates:
    - For `developer`: `/home/ubuntu/worktrees/developer-<issue_id>` (reused across developer rework rounds on the same issue so branch history and local caches persist).
    - For `planner` and `evaluator`: a fresh, clean detached worktree `/home/ubuntu/worktrees/<role>-<run_id[:8]>` at `ref` (`head_sha`), guaranteed free of `.nanobot/`, `memory/`, `.atobar/research/`, or uncommitted files from `developer`.
  - [ ] **AC-2:** Setup commands (`setup_cmds`) run once on the shared base environment (or once per new worktree if repo-local) with a `.atobar/.setup_complete_<sha>` marker so warm transitions skip redundant `pip`/`uv` installs.
  - [ ] **AC-3:** `WORKSPACE_DIR` environment variable is updated per role turn so `workspace_dir()`, `vision_tools`, `fs_tools`, and `nanobot_config` all operate strictly inside the active role's worktree.

### WP-4: Synchronous Control Panel Turn Handover & Serverless Arbitrator (`control_panel/app.py`, `conductor/io.py`)
- **Goal:** Add `POST /v1/runs/{run_id}/turn-complete` on the Control Panel so a warm Spot instance can report its completed envelope (`flow-plan`, `flow-ack`, `flow-verdict`) and synchronously claim the next role's `(run_id, run_credential, role, head_sha)` without triggering `flow_dispatcher.yml` to launch a new EC2 Spot VM. Also update `ingest_event` (`POST /v1/events`) to use `assess_issue_sufficiency()` when choosing the initial role (`planner` vs `developer`) on `issues.labeled`.
- **Scope:**
  - `src/control_panel/app.py`
  - `src/flow_engine/conductor/io.py` (`process_flow_event`, `apply_actions`, `rebuild_loop_state`)
  - `tests/control_panel/test_turn_complete.py` (new), `tests/conductor/test_io.py`
- **Out of scope:** Spot instance `main.py` supervisor loop (WP-5).
- **Acceptance Criteria (tests first):**
  - [ ] **AC-1:** On `issues.labeled` (`flow:feature`, `flow:fix`, `flow:docs`, `flow:plan`), `ingest_event` fetches the issue body (or receives it in payload), runs `assess_issue_sufficiency()`, and dispatches `initial_role` (`"planner"` or `"developer"`).
  - [ ] **AC-2:** `POST /v1/runs/{run_id}/turn-complete` authenticates `FLOW_RUN_CREDENTIAL`, invokes `process_flow_event()` with the same `event_id` that GitHub's `issue_comment.created` webhook will compute (`compute_event_id`), and when `apply_actions()` produces a `DispatchAction`:
    - Mints `(next_run_id, next_run_cred)` for `act.role`,
    - Updates `sessions` for the calling `instance_id` to `run_id = next_run_id, role = act.role`,
    - Does **not** call `gh.dispatch_flow_run()`, and
    - Returns `{"action": "continue", "role": act.role, "run_id": next_run_id, "run_credential": next_run_cred, "head_sha": act.head_sha}`.
  - [ ] **AC-3:** When the delayed `issue_comment.created` webhook from `flow-trigger.yml` arrives at `POST /v1/events` / `process_flow_event()` for a comment already processed via `turn-complete`, `store.has_processed_event(tenant.id, event_id)` deduplicates it so no second Spot VM is spawned.
  - [ ] **AC-4:** When `apply_actions()` produces no further `DispatchAction` (e.g., escalated, converged, or waiting on external CI checks after `evaluator` approval), `turn-complete` returns `{"action": "shutdown", "reason": new_state.status}`.
  - [ ] **AC-5:** When an external `check_run.completed` event causes convergence after the Spot VM has already shut down, `apply_actions()` executes `run_arbitrator()` in-process (or dispatches a lightweight runner) without requiring a new Spot VM when no live session is active.

### WP-5: Warm Spot Session Supervisor Loop (`src/flow_engine/main.py`)
- **Goal:** Upgrade `src/flow_engine/main.py` from a single-role script into a multi-turn **Warm Spot Session Supervisor** that loops across role assignments returned by `POST /v1/runs/{run_id}/turn-complete` (`planner` → `developer` → `evaluator` → `developer` R2 → `arbitrator`), switching worktrees and credentials cleanly on each turn and calling `_shutdown()` only when `action == "shutdown"` or a circuit breaker trips.
- **Scope:**
  - `src/flow_engine/main.py`
  - `src/flow_engine/roles/planner/runner.py`
  - `src/flow_engine/roles/developer/runner.py`
  - `src/flow_engine/roles/evaluator/runner.py`
  - `tests/test_main_supervisor.py` (new)
- **Out of scope:** Terraform/AMI changes.
- **Acceptance Criteria (tests first):**
  - [ ] **AC-1:** `run_planner`, `run_developer`, and `run_evaluator` return a structured `TurnOutcome(envelope_text, comment_id, head_sha, usage)` after posting their envelope comment.
  - [ ] **AC-2:** `main.py` runs a bounded supervisor loop (`max_turns = 12`): after each role turn completes, it calls `POST /v1/runs/{run_id}/turn-complete`. If the response is `action == "continue"`, it rotates `os.environ["FLOW_ROLE"]`, `FLOW_RUN_ID`, `FLOW_RUN_CREDENTIAL`, `FLOW_HEAD_SHA`, and `WORKSPACE_DIR` (via `prepare_role_worktree()`), re-initializes `SessionRegistry` / `GitHubTelemetryUI` for the new role turn, and executes the next role immediately on the same VM.
  - [ ] **AC-3:** If `turn-complete` returns `action == "shutdown"` (or is unreachable / returns an error, falling back to async GHA Conductor), `main.py` exits the loop and executes `_shutdown()` (`sudo shutdown -h now`), preserving 100% backward compatibility with single-shot Spot dispatch.

### WP-6: E2E Verification & Documentation Sync (`docs/SWARM_ARCHITECTURE.md`, `docs/ROADMAP.md`)
- **Goal:** Verify both paths (Path A: Fast-Track well-specified issue `developer → evaluator → arbitrator` on 1 Spot VM; Path B: Underspecified issue `planner → developer → evaluator → arbitrator` on 1 Spot VM) with unit/integration tests and update `docs/SWARM_ARCHITECTURE.md` and `docs/ROADMAP.md`.
- **Scope:**
  - `tests/e2e/test_warm_spot_handover.py` (new deterministic multi-role integration test)
  - `docs/SWARM_ARCHITECTURE.md`, `docs/ROADMAP.md`
- **Acceptance Criteria:**
  - [ ] **AC-1:** Multi-role integration test proves a 2-round trajectory (`planner` → `developer` R1 → `evaluator` R1 (`changes_requested`) → `developer` R2 → `evaluator` R2 (`approved`) → `arbitrator`) completes inside a single supervisor invocation with isolated worktrees per role and zero duplicate `dispatch_flow_run` calls.
  - [ ] **AC-2:** `PYTHONPATH=src:tests/control_panel uv run --extra agents pytest tests/ -q`, `uv run ruff check src/ tests/`, and `uv run mypy src/ --ignore-missing-imports` pass with zero errors.

---

## Verification Commands
```bash
PYTHONPATH=src:tests/control_panel uv run --extra agents pytest tests/protocol/test_envelopes.py tests/conductor/ tests/roles/ tests/runtime/test_sandbox.py -q
uv run ruff check src/ tests/
uv run mypy src/ --ignore-missing-imports
```

---

## Risks & Mitigations
- **Risk 1 — State/Credential Bleed Across Roles on Shared VM:** Mitigated by (a) isolated per-role `git worktree` directories (`/home/ubuntu/worktrees/<role>-<id>`), (b) `nanobot` `restrictToWorkspace: True` jailing file tools to the active worktree, (c) purging `FLOW_LLM_API_KEY*` env vars between turns before fetching the next role's `FLOW_RUN_CREDENTIAL`, and (d) storing `nanobot` config/session files under `/home/ubuntu/flow-run/<run_id>/`.
- **Risk 2 — Webhook vs. Synchronous `turn-complete` Race:** Mitigated by computing the exact same deterministic `event_id = compute_event_id(raw_event)` in `turn-complete` and recording it in `processed_events` before returning, so the subsequent GitHub `issue_comment.created` webhook is idempotently ignored (`duplicate event`).
- **Risk 3 — Spot Preemption Mid-Pipeline:** Because every role still posts its `flow-*` envelope comment to GitHub and `WipSyncer` still pushes `flow-wip/<role>/<tenant>/<issue>` (Axiom 6 & Axiom 12), if AWS reclaims the Spot instance mid-run, the Conductor's existing stale-session reaper (`reap_stale_sessions` in `src/flow_engine/conductor/gc.py`) and webhook fallback remain intact.
