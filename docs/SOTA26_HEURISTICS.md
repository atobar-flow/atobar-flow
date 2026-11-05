# SOTA'26 AI SWE Heuristics (The Core Axioms & Manifesto)

#### 1. File Editing: Exact String Replacement (The Optimistic Lock)

* **Axiom:** LLMs hallucinate line numbers and ASTs break on intermediate syntax errors. Unified diffs (`patch`) and AST manipulation are strictly prohibited.
* **Implementation:** The file editor MUST be a Python `MultiEdit` tool taking an array of `{old_string, new_string}` pairs.
* **Abstractions:** The orchestrator must enforce an optimistic lock: `if file_content.count(old_string) != 1: raise Exception("Edit Failed: old_string is not globally unique in the file")`. The tool fails safely before code corruption occurs.

#### 2. Command Execution & Head Truncation

* **Axiom:** Secondary LLM log summarizers introduce latency and cost. 50,000-line logs blow out context windows.
* **Implementation:** The `exec` tool must multiplex interactive PTY sessions via Python asyncio with a deque[bytes] 1MB ring buffer per shell. Sessions are in-process (no network hop) because the orchestrator and sandbox coexist on the same EC2 instance. If future architecture requires cross-machine orchestration (e.g., cheap control plane → GPU sandbox), this layer can be promoted to a WebSocket transport without changing the tool interface.
* **Abstractions:** Output returned to the LLM must be strictly head-truncated at `4000` characters (configurable via central `.config`). The orchestrator must append `<truncation_notice path="/tmp/overflow_<id>.log">` and write the full stream to disk. The agent must proactively invoke `ripgrep` (`rg`) on the overflow file to trace errors.

#### 3. Zero-Blast-Radius Authentication (Zero-Prompt Secrets)

* **Axiom:** Shared PATs exposed to LLM context windows are catastrophic prompt-injection risks.
* **Implementation:** Auth is handled at Phase 0 OS Bootstrapping.
* **Abstractions:** The AWS `cloud-init` script assumes an IAM Instance Profile, fetches the GitHub token from AWS SSM Parameter Store/Secrets Manager, writes it to `~/.git-credentials`, runs `git config --global credential.helper store`, and unsets the variable. The AI agent interacts using pure `https://github.com` URLs, remaining completely blind to the auth mechanism.
* **Swarm extension:** each role (DEVELOPER, EVALUATOR, ARBITRATOR) has its **own** GitHub App and IAM instance profile, and may read only its own SSM parameter. No secret is ever placed in a prompt, a PR comment or a log (*Zero-Prompt Secrets*). Role identity is enforced by GitHub authorship, not by prompt text.

#### 4. Context Ingestion (Anti-RAG)

* **Axiom:** VectorDBs and embedding pipelines are probabilistic and inefficient for strict code structures.
* **Implementation:** Code navigation relies on deterministic tools (`ripgrep`, `tree`, windowed `read_file`).
* **Abstractions:** High-level project knowledge (`./.atobar/wikis/`) is parsed offline into a strict 4-section schema (Purpose, Technologies, Structure, Concepts). These condensed notes are injected statically into the Gemini Context Caching API at session boot.

#### 5. Phase 0 Deterministic Scaffolding (Shift-Left Determinism)

* **Axiom:** AI reasoning tokens are expensive. Do not spend them on environment setup.
* **Implementation:** The AWS Spot Instance `cloud-init` runs `./.atobar/setup.sh` entirely in the background. A role's LLM loop is only initialized after the environment is fully built, dependencies are installed, and tests are ready to be run.
* **Shift-Left Determinism:** anything that *can* be decided by code MUST be decided by code before an LLM is consulted. This covers environment setup, dispatch, round counting, caps, convergence, metric gates, envelope validation and author binding. LLMs are reserved for open-ended engineering and evidence gathering.

#### 6. Stateless Spot Preemption Recovery

* **Axiom:** AWS EC2 Spot Instances are volatile. Complex block-storage (EBS) snapshotting is out-of-scope bloat.
* **Implementation:** State recovery is Git-based.
* **Abstractions:** The agent maintains its cognitive plan via a strict `todo_write` state machine. The orchestrator periodically pushes headless, atomic commits containing file diffs and state to a hidden remote repository branch (`flow-wip/<role>/<tenant>/<issue>`; WIP state is per role and is never read by another role). Upon instance preemption, the next provisioned Spot VM clones the WIP branch, reads the `todo` state appended to the GitHub Issue comment, and resumes execution.

#### 7. Multimodal Vision Testing (SOTA YOLO)

* **Axiom:** Relying strictly on DOM parsing misses CSS layout shifts and Z-index collisions.
* **Implementation:** The base Ubuntu AMI is provisioned to automatically boot `Xtigervnc` (display `:0`), `websockify` (port 6080), and Chrome running with `--use-angle=swiftshader-webgl` (software GPU rendering) and CDP active on port `29229`.
* **Abstractions:** The agent invokes multimodal vision by capturing the native X11 framebuffer (pixels) and combining it with serialized Chrome DevTools Protocol (CDP) DOM annotations.

#### 8. Circuit Breakers & Definition of Done

* **Axiom:** LLM prompt instructions ("stop after 3 tries") are ignored during failure spirals. Agents iterate within their role, but termination is decided by code.
* **Implementation (per instance):** the Python runtime enforces hard, OS-level kill switches from `flow.config.json` (`max_ci_retries`, `max_wall_clock_minutes`). When one trips, the instance self-terminates.
* **Implementation (per PR loop):** the deterministic CONDUCTOR enforces the tenant's `loop_limits`:
  * `max_review_rounds`: one round = one EVAL verdict on a distinct `head_sha`
  * `max_repeat_finding`: the same finding fingerprint in N consecutive rounds
  * `max_pr_wall_clock_minutes` and `max_pr_cost_usd`

  When any cap trips, the CONDUCTOR posts `flow-escalation`, applies the `flow:escalated` label and stops all dispatches.
* **Definition of Done:**
  1. Tests first: failing acceptance tests are committed before the implementation.
  2. External GitHub Actions CI on `head_sha` is the mathematical proof; agent-reported output is never evidence.
  3. An EVALUATOR `approved` verdict on the same SHA (Axiom 11).
  4. A DEVELOPER ack with no disputes.
  5. A passing ARBITRATOR business gate (Axiom 12).

#### 9. PR as Sole Documentation Artifact

* **Axiom:** AI executor sessions are ephemeral — once the PR is submitted, the session context is destroyed. The PR description is the only persistent record of intent, reasoning, and verification.
* **Implementation:** Every PR must follow the structured template in `.github/PULL_REQUEST_TEMPLATE.md`. The agent MUST fill all sections. Empty or one-line descriptions are a build failure equivalent.
* **Abstractions:** The `AGENTS.md` file at repo root defines the full behavioral contract. The PR template enforces structure. Together they ensure that any future agent (or human) reviewing the PR can understand the change without access to the original session logs.

#### 10. Central Control Plane / Session Registry (ADR) — Multi-Tenant

* **Axiom:** Decentralized state (per-issue comments) limits observability and multi-agent coordination. A central registry provides a unified "fleet view" without replacing durable Git-based state recovery.
* **Implementation:** A `SessionRegistry` Python module and `EventSink` report telemetry to the FastAPI Control Panel via authenticated run credentials. Agents hold zero direct database or Supabase credentials.
* **Abstractions:**
  - **Layering:** The hidden `flow-wip/<role>/<tenant>/<issue>` Git branch remains the sole source of truth for durable recovery state (Axiom 6). The Control Panel provides real-time dashboarding, session tracking, and append-only event auditing.
  - **Schema:** The `sessions` table tracks latest mutable state (`instance_id`, `target_repo`, `issue_id`, `status`, `started_at`, `heartbeat_at`, `last_activity_at`, `current_todo`, `last_tool`, `cost_estimate`, `iterations`, `usage`, `agent_runtime`, `head_sha`). The `run_events` table stores the immutable append-only audit trail.
  - **Lifecycle:** The agent performs an initial register on boot. Lightweight heartbeats update `sessions` via `POST /v1/runs/{run_id}/heartbeat`. Granular tool/model lifecycle events flow to `POST /v1/runs/{run_id}/events`. The session transitions to a terminal status on clean exit or circuit-breaker termination.
  - **Swarm extension:** every run carries `role` and `tenant_id`. Agents authenticate exclusively via ephemeral, per-run credentials through the Control Panel API; only the CONDUCTOR and Control Panel service role access Supabase directly.

#### 11. Evaluator-Optimizer Separation (The Different Checker Rule)

* **Axiom:** An agent that verifies its own work suffers confirmation bias. Shared memory between the author and the checker turns the Evaluator-Optimizer pattern into self-grading.
* **Implementation:** DEVELOPER and EVALUATOR are separate processes on separate Spot VMs. Each has its own GitHub App identity, IAM profile, WIP branch and tool allowlist (EVAL has no write or push tools). EVAL may use a different model configuration.
* **Abstractions:** EVAL's context is limited to the issue's acceptance criteria, the diff at `head_sha`, external CI results and its own observations (X11 framebuffer + CDP DOM, Axiom 7). The PR description is passed in as *untrusted claims to verify*. DEV transcripts are never shared.

#### 12. Stigmergic Coordination (PR Thread as Append-Only Log)

* **Axiom:** Direct agent-to-agent chat is unobservable, unreplayable and easy to inject into. Coordination must leave durable, auditable traces.
* **Implementation:** Roles communicate only through fenced `flow-ack` / `flow-verdict` / `flow-arbitration` / `flow-escalation` JSON envelopes in PR comments, plus `flow/eval` and `flow/arbiter` check runs. Each envelope is bound to one `head_sha` and is valid only when authored by the matching role's App.
* **Abstractions:** The CONDUCTOR is a deterministic, event-driven state machine (no LLM) that parses envelopes, counts rounds, enforces Axiom 8 caps and dispatches the next role. Each agent run is a stateless reaction to the latest valid envelope.

#### 13. Deterministic Business Arbitration (Anti-Metric-Drift)

* **Axiom:** An LLM reading CI logs will eventually hallucinate a pass. Business alignment must be measured, not narrated.
* **Implementation:** Each tenant declares its metrics, thresholds, baselines and `arbitration.mode` (`shadow` | `recommend` | `auto_merge`) in `tenants/<owner>__<repo>.json`. The tenant's CI publishes `flow-metrics.json` as a check-run output.
* **Abstractions:** `arbitrate(policy, observations) -> Decision` is a pure, unit-tested function. An observation is admissible only if all of the following hold:
  * it was fetched by the ARBITRATOR from the Checks API for the exact `head_sha`;
  * it was produced by the manifest's `workflow_path` and triggered by a GitHub event rather than a `flow-*` bot;
  * it is schema-valid;
  * it is digest-recorded in append-only `metric_observations`.

  A PR that modifies the metric workflow or the tenant manifest is always `blocked_requires_human`. An LLM may narrate a `Decision`, but never produce one.
