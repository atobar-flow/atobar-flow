# atobar-flow roadmap and implementation status

This document tracks what is built in code today and the milestone history (`M01`–`M11` and `J34`–`J51`). For product requirements and user journeys see [`docs/PRD.md`](PRD.md); for the system architecture and database ownership see [`docs/HLD.md`](HLD.md) and [`docs/SWARM_ARCHITECTURE.md`](SWARM_ARCHITECTURE.md).

## What we are building (one paragraph)

A remote, multi-tenant software factory. A repository admin connects a Client Repo once from a laptop
([`scripts/onboard.sh`](https://github.com/atobar-flow/atobar-flow/blob/main/scripts/onboard.sh)); from then on atobar-flow runs everything on its own
infrastructure. A deterministic CONDUCTOR (no LLM) dispatches an isolated DEVELOPER, then an independent EVALUATOR
(different VM, GitHub App and IAM identity) on the exact PR SHA, loops them through append-only `flow-*` envelopes in
the PR thread under hard caps (4 rounds, repeat-finding 2, 240 min, $25 per PR), and finally a deterministic
ARBITRATOR gates the converged PR on the tenant's business metrics in the tenant's mode (`shadow` → `recommend` →
`auto_merge`). Metric definitions and thresholds live in atobar-flow (`tenants/*.json`), never in the Client Repo;
client CI only produces measurements with provenance. Each role's agent core runs behind a pluggable `AgentRuntime`
— `native` (in-process `llm_loop`) or `nanobot` (`nanobot-ai` SDK, flow tools over a stdio MCP server) — chosen per
tenant by `loop.agent_runtime`. Above the per-PR loop sits a **goal loop**: the tenant's
target state (objective, target metrics, guardrails, budget) lives in its manifest; after every merge the CONDUCTOR
re-measures the default branch and, until the targets are met or the goal budget or stall limit stops it, dispatches a
PLANNER that may only open the next issues. All of this code lives in this repository.

## Status (as built)

| Area | State | Where |
|---|---|---|
| Supabase schema: tenants, wikis, metric definitions, append-only observations, loop_rounds, tenant tokens, encrypted LLM keys | **Done** | `infra/supabase/migrations/`, applied by `supabase-migrate.yml` |
| Control Panel API + UI at <CP_PUBLIC_URL> (Hetzner cx23, Caddy TLS, pull-based deploy every 5 min) | **Done** | `control-panel/src/control_panel/`, `infra/control-panel/`, `infra/terraform/` |
| GitHub sign-in; tenant visibility from repo permissions; admins manage tokens and keys | **Done** | `control-panel/src/control_panel/web.py` |
| Tenant tokens (`atb_…`, only sha256 stored) and `GET /v1/tenant` | **Done** | `control-panel/src/control_panel/tokens.py`, `app.py` |
| KMS envelope-encrypted BYOK keys (Gemini, Claude, OpenAI, DeepSeek, Grok), bound to `(tenant_id, provider)` AAD | **Done** | `control-panel/src/control_panel/llm_keys.py`, `flow-agent/src/flow_agent/runtime/key_delivery.py` |
| Onboarding script incl. empty repositories ("build from zero") | **Done** | `scripts/onboard.sh` |
| Generated client trigger; control labels never dispatch; work labels dispatch only for `active` tenants | **Done** | `control-panel/src/control_panel/client_templates.py`, `POST /v1/events` |
| Metric ingestion with provenance (`POST /v1/metrics`) | **Done** | `control-panel/src/control_panel/app.py`, `flow-engine/src/flow_engine/api.py` |
| Tenant manifest (`TenantManifest`) and `onboarder` Spot role | **Done** | `contracts/src/atobar_contracts/manifest.py`, `flow-agent/src/flow_agent/roles/onboarder/` |
| Evaluation spec parser (`parse_spec`, CLI) | **Done** | `flow-engine/src/flow_engine/tenancy/spec.py` (M01feat01) |
| `flow:onboard` opens onboarding PR and synthesizes `SotaWiki` | **Done** | `flow-engine/src/flow_engine/api.py`, `flow-agent/src/flow_agent/roles/onboarder/` (M01feat02, J50) |
| `flow:approved` permission check and developer dispatch | **Done** | `contracts/src/atobar_contracts/conductor_io.py` (M01feat03) |
| Tenant auto-activation on first default-branch observation | **Done** | `flow-engine/src/flow_engine/api.py` (M01feat04) |
| Issue state labels, transitions and status card | **Done** | `contracts/src/atobar_contracts/issue_states.py` (M01feat05) |
| Envelope models, parser, renderer, author binding, finding fingerprint | **Done** | `contracts/src/atobar_contracts/envelopes.py` (M02feat01) |
| Tenant manifest v2 and checked-in JSON schemas | **Done** | `contracts/src/atobar_contracts/manifest.py`, `schemas/` (M02feat02) |
| Conductor `step()` pure function, convergence, circuit breakers | **Done** | `contracts/src/atobar_contracts/machine.py` (M02feat03) |
| Arbitrator `arbitrate()` deterministic metric gate with provenance | **Done** | `contracts/src/atobar_contracts/arbitration.py`, `flow-agent/src/flow_agent/roles/arbitrator/gate.py` (M02feat04) |
| Provider-neutral `LLMClient` with Gemini adapter | **Done** | `flow-agent/src/flow_agent/llm/` (M03feat01) |
| Generic `llm_loop` and `RoleSpec` extracted from `react_loop` | **Done** | `flow-agent/src/flow_agent/runtime/llm_loop.py`, `flow-agent/src/flow_agent/roles/base.py` (M03feat02) |
| Per-role tool allowlists | **Done** | `flow-agent/src/flow_agent/runtime/tools.py` (M03feat03) |
| Per-role WIP state (`WipSyncer`) | **Done** | `flow-agent/src/flow_agent/runtime/wip.py` (M03feat04) |
| Role sandbox bootstrap (`prepare` under timeouts) | **Done** | `flow-agent/src/flow_agent/runtime/sandbox.py` (M03feat05) |
| Forward PR, comment and check events from client repos | **Done** | `control-panel/src/control_panel/app.py`, `flow-engine/src/flow_engine/api.py` (M04feat03) |
| Single app identity (GitHub App, IAM instance profile, SSM keys) | **Done** | `infra/terraform/identity/` (M04feat01, fixed 2026-10-05 J13) |
| Role-aware Spot dispatch | **Done** | `flow-engine/src/flow_engine/conductor/dispatch.py`, `contracts/src/atobar_contracts/github_rest.py` (M04feat02, J34) |
| Conductor service: load PR state, apply `step()`, record `loop_rounds` | **Done** | `flow-engine/src/flow_engine/api.py`, `contracts/src/atobar_contracts/conductor_io.py` (M04feat04, J34) |
| Deliver the tenant's LLM key to its role sandbox for one run | **Done** | `control-panel/src/control_panel/app.py`, `flow-agent/src/flow_agent/runtime/key_delivery.py` (M04feat05, fixed 2026-10-05 J07) |
| DEVELOPER role (RoleSpec, prompt, ack completion) | **Done** | `flow-agent/src/flow_agent/roles/developer/` (M05feat01) |
| EVALUATOR role (RoleSpec, checks wait, verdict, flow/eval check) | **Done** | `flow-agent/src/flow_agent/roles/evaluator/` (M05feat02, fixed 2026-10-05 J03) |
| Evaluator visual evidence: headless Chrome/X11 screenshots as artifacts | **Done** | `flow-agent/src/flow_agent/roles/evaluator/visual.py` (M05feat03) |
| ARBITRATOR role (verified observations, flow/arbiter check per mode, metrics on PRs) | **Done** | `flow-agent/src/flow_agent/roles/arbitrator/`, `contracts/src/atobar_contracts/conductor_io.py` (M05feat04) |
| Treat client content as untrusted input (preambles, wrapping, egress allowlist) | **Done** | `flow-agent/src/flow_agent/core/untrusted.py`, `infra/terraform/` (M06feat01, J44) |
| Instance lifecycle: GC and stale-session reaper | **Done** | `flow-engine/src/flow_engine/conductor/gc.py` (M06feat02, J34) |
| Per-role tracing (TraceWriter, role+tenant events, evaluator isolation) | **Removed** | Deleted in #283; replaced by unified run_events stream |
| Client CI auth via GitHub Actions OIDC | **Done** | `control-panel/oidc.py` (M07feat01) |
| LLM adapters for Claude, DeepSeek and Grok | **Done** | `flow-agent/src/flow_agent/llm/` (M07feat02) |
| Flexible evaluation spec table normalization and localized column aliases | **Done** | `flow-engine/tenancy/spec.py` (M07feat07) |
| Wiki generation refactor (tenant-scoped, read-only DoD exception) | **Done** | `flow-agent/src/flow_agent/knowledge/onboard_repo.py` (M07feat04) |
| uv standardisation (single dependency source, hermetic installs, client contract) | **Done** | `pyproject.toml`, `.atobar/setup.sh`, `.github/workflows/ci.yml` (M07feat05, partial 2026-10-05 J11) |
| Control Panel: loop history views (rounds, cost, escalations) | **Done** | `control-panel/web.py` (M07feat06, populated with real usage and envelope digests via #283) |
| Control Panel: hardened container build with strict security scanners | **Done** | `infra/control-panel/`, `.github/workflows/control_panel_build.yml` (M07feat03) |
| Pluggable `AgentRuntime` seam (`RoleTask`/`RoleResult`, `UsageLedger`, `HaltReason`, runtime registry) | **Done** | `flow-agent/src/flow_agent/agents/` (M09feat01) |
| Flow tools over stdio MCP, per-role allowlists enforced server-side | **Done** | `flow-agent/src/flow_agent/tools_mcp/` (M09feat02) |
| `NanobotRuntime` (`nanobot-ai` SDK headless; FlowHook: heartbeats, stall detector, usage ledger, crash flush, provider-error retry/classify) | **Done** | `flow-agent/src/flow_agent/agents/nanobot_runtime.py`, `nanobot_config.py`, `errors.py` (M09feat03) |
| Role runners dispatch via `get_runtime`; `TenantLLMKey` key delivery | **Done** | `flow-agent/src/flow_agent/roles/*/runner.py`, `flow-agent/src/flow_agent/runtime/key_delivery.py` (M09feat04) |
| `loop.agent_runtime` end-to-end: manifest → conductor/CP payloads → dispatcher → cloud-init `--extra agents`; `loop_rounds.usage_detail`, structured halt, GC tombstone | **Done** | `flow-engine/tenancy/manifest.py`, `flow-engine/conductor/`, `infra/supabase/migrations/0010_usage_detail.sql` (M09feat05, usage read and populated from sessions in #283) |
| EVALUATOR on nanobot: visual tools via MCP, verdict path | **Done** | `flow-agent/src/flow_agent/roles/evaluator/` (M09feat06) |
| E2E pilot on `nanobot` (ollama-monitor) | **Done** | Consolidated E2E pilot across `shadow` and `recommend` modes on warm Spot `nanobot` runtime (#167, #138, #247) |
| Multi-provider BYO keys: canonical provider map, `llm` manifest block, `/llm-key` `keys` fan-out, `FLOW_LLM_API_KEY_<PROVIDER>` → nanobot providers map | **Done** | `flow-agent/src/flow_agent/llm/providers.py`, `key_delivery.py`, `nanobot_config.py` (M09feat08) |
| Telemetry overhaul: collapse to one `run_events` stream, fix pipes, delete dead telemetry | **Done** | `run_events` table, CP `/v1/runs/{id}/events`, `EventSink` (M11feat01 #283) |
| Onboard arbitrary models + per-provider keys via Control Panel UI | **Done** | CP web `llm_keys` + `tenant_role_llm` surface (#266) |
| Provider/model usage surfacing: per-call `usage_detail` records into CP loop history | **Done** | CP web reads `loop_rounds.usage_detail` (#167) |
| Additional `AgentRuntime` backends (e.g. claude-agent-sdk, codex) | Planned | register in `agents/registry.py`, reuse `tools_mcp` |
| Optional Langfuse OTLP sink for nanobot sessions | Planned | `nanobot_runtime.py` |
| Tenant goal: target state, targets vs guardrails, goal budget | **Done** | `contracts/src/atobar_contracts/goal.py`, `0022_tenant_goals.sql` (M05feat05 #204) |
| Arbitrator: targets judged by progress, guardrails by threshold | **Done** | `contracts/src/atobar_contracts/arbitration.py`, `flow-agent/src/flow_agent/roles/arbitrator/gate.py` (M05feat06 #204) |
| PLANNER role (`flow-plan` Task Ledger, Spec-Sufficiency Gate, `SotaWiki` injection, warm Spot worktree handover) | **Done** | `flow-agent/src/flow_agent/roles/planner/`, `conductor/sufficiency.py`, `runtime/sandbox.py` (J38 #311) |
| Conductor goal loop: re-measure after merge, plan until met or stopped | **Done** | `flow-engine/src/flow_engine/conductor/goal.py` (M05feat08 #204) |
| Two-stage Evaluator review: Stage 1 static/contract short-circuit (`file:<path>#L<start>-L<end>`) + progressive-disclosure verdict summary | **Done** | `flow-agent/src/flow_agent/roles/evaluator/spec.py`, `prompt.py` (J39 #317) |
| Zero-trust Spot security hardening: envelope `run_id` binding, `WARM_HANDOVER_FORBIDDEN_ROLES`, role-scoped tokens, KMS fail-closed, untrusted wrapping | **Done** | `contracts/src/atobar_contracts/conductor_io.py`, `flow-engine/src/flow_engine/api.py`, `flow-agent/src/flow_agent/core/untrusted.py` (J42–J44 #341–#345) |
| CI FinOps & single billable CI job with combined mypy and checksummed binaries | **Done** | `.github/workflows/ci.yml`, `atobar-ops/src/atobar_ops/verify.py` (J45 #347, J47 #363) |
| Janitor-sourced verifiable price catalog (`price_snapshots`, `llm_prices`, `ec2_instance_costs`) & Spot cost settlement | **In PR** | `J48` (#360 / PR #364) |
| Unified `@atobar-flow-agent` conversational steering (Issue = Plan, PR = Correction) & X11 screenshot provenance (`xwininfo`, `ps`, `data_mode`) | **Done** | `contracts/src/atobar_contracts/conductor_io.py`, `flow-agent/src/flow_agent/tools/vision_tools.py` (J49 #366) |
| `SotaWiki` Mermaid architecture synthesis (`validate_mermaid_diagram`), coalesced auto-refresh queue, and Control Panel repo screen | **Done** | `contracts/src/atobar_contracts/wiki.py`, `flow-agent/src/flow_agent/knowledge/onboard_repo.py`, `0026_wiki_lifecycle.sql` (J50 #369) |
|Canonical `docs/PRD.md`, `docs/HLD.md` & `https://github.com/atobar-flow` scrollytelling spec | **Done** | `docs/PRD.md`, `docs/HLD.md` (J51 #370) |
| Granular BYOK key management & cost sharing (per-invoking-user key routing on top of per-repo, per-role routing) | Planned | `tenant_llm_keys.contributor_github_login`, `POST /v1/runs/{id}/llm-key` (`docs/PRD.md` FR-5, `docs/HLD.md` §4.1) |
| Legacy single-agent path `flow-agent/src/flow_agent/agent/` | **Done** | Deleted in M06feat06 |

Spec vs code notes:

- `TenantManifest` (`contracts/src/atobar_contracts/manifest.py`) is managed via the Control Panel and audited in `tenants_manifest_history`; `schemas/tenant.schema.json` is the checked-in contract.
- Metrics reach atobar-flow through `POST /v1/metrics` (authenticated by the tenant token or GitHub OIDC, recorded append-only in `metric_observations`). The `ARBITRATOR` re-verifies each observation against the GitHub Actions run before trusting it.
- `flow-engine` (`flow-engine/src/flow_engine/conductor/dispatch.py` and `contracts/src/atobar_contracts/github_rest.py`) launches role Spot sandboxes directly via AWS EC2 API.

## Critical path

```
M01 onboarding completion ──────────────────────────────┐
M02feat01 envelopes ─┬─ M02feat03 conductor ─┐           │
M02feat02 manifest ──┴─ M02feat04 gate ──────┼───────────┤
M03feat01 LLMClient → M03feat02 loop → feat03/04/05 ──┐  │
M04feat01 identities → M04feat02 dispatch ─┬─ M04feat04 conductor workflow → M05feat01 DEV → M05feat02 EVAL → M06feat04 pilot shadow → M06feat05 recommend → M06feat06 remove legacy
M04feat03 PR events ───────────────────────┘          M05feat04 ARBITRATOR ┘

Goal loop (blocks the pilot): M05feat05 goal ─┬─ M05feat06 progress gate ─┬─ M05feat08 goal loop → M06feat04 pilot shadow
                                              └─ M05feat07 PLANNER ───────┘
```

Rule: an issue may start when every **Blocked by** issue is closed. Issues with no blockers (M01feat00/01,
M02feat01/02, M03feat01, M04feat01) can run in parallel today.

### M01: Onboarding completion: spec issue to active tenant

| Key | Issue | Title | Blocked by | Owner |
|---|---|---|---|---|
| M01feat00 | #137 | [human] Onboard ollama-monitor with atobar-init | - | human |
| M01feat01 | #161 | Parse an evaluation spec issue into a draft TenantManifest | - | factory |
| M01feat02 | #147 | flow:onboard opens the tenant manifest PR and moves the issue to flow:needs-approval | M01feat01 | factory |
| M01feat03 | #162 | flow:approved: permission-checked approval, then registration and the client metrics PR | M01feat02 | factory |
| M01feat04 | #163 | Activate a tenant automatically on its first default-branch observation | M01feat03 | factory |
| M01feat05 | #148 | Issue state labels and a pinned status card | M01feat03 | factory |

### M02: Swarm core, deterministic (no LLM)

| Key | Issue | Title | Blocked by | Owner |
|---|---|---|---|---|
| M02feat01 | #120 | Envelope models, parser and renderer with author binding | - | factory |
| M02feat02 | #121 | Tenant manifest v2 and checked-in JSON schemas | - | factory |
| M02feat03 | #128 | Conductor step(): convergence and every cap, as a pure function | M02feat01, M02feat02 | factory |
| M02feat04 | #131 | Arbitrator arbitrate(): deterministic metric gate with provenance checks | M02feat02 | factory |

### M03: Role runtime

| Key | Issue | Title | Blocked by | Owner |
|---|---|---|---|---|
| M03feat01 | #164 | Provider-neutral LLMClient with a Gemini adapter | - | factory |
| M03feat02 | #123 | Extract llm_loop and RoleSpec from react_loop | M03feat01 | factory |
| M03feat03 | #125 | Per-role tool allowlists | M03feat02 | factory |
| M03feat04 | #124 | Per-role WIP state (WipSyncer) | M03feat02 | factory |
| M03feat05 | #110 | Role sandbox bootstrap: clone the tenant repo and run its setup under timeouts | M03feat02 | factory |

### M04: Isolation and dispatch

| Key | Issue | Title | Blocked by | Owner |
|---|---|---|---|---|
| M04feat01 | #126 | Single app identity: GitHub App, IAM instance profile, SSM keys | - | factory + human |
| M04feat02 | #127 | Role-aware Spot dispatch | M04feat01 | factory |
| M04feat03 | #165 | Forward PR, comment and check events from client repos | M02feat01 | factory |
| M04feat04 | #166 | Conductor workflow: load PR state, apply step(), record loop_rounds | M02feat03, M04feat02, M04feat03 | factory |
| M04feat05 | #151 | Deliver the tenant's LLM key to its role sandbox for one run | M03feat01, M04feat02 | factory |

### M05: Roles: developer, evaluator, arbitrator

| Key | Issue | Title | Blocked by | Owner |
|---|---|---|---|---|
| M05feat01 | #129 | DEVELOPER role | M03feat03, M03feat04, M03feat05, M04feat04, M04feat05 | factory |
| M05feat02 | #130 | EVALUATOR role: exact-SHA checkout, CI wait, verdict and flow/eval check | M05feat01 | factory |
| M05feat03 | #113 | Evaluator visual evidence: headless Chrome/X11 screenshots as artifacts | M05feat02 | factory |
| M05feat04 | #132 | ARBITRATOR role: verified observations, flow/arbiter check per mode, metrics on PRs | M02feat04, M04feat04 | factory |
| M05feat05 | #201 | Tenant goal: target state, targets vs guardrails, goal budget | - | factory |
| M05feat06 | #202 | Arbitrator: judge targets by progress, guardrails by threshold or no-regression | M05feat05 | factory |
| M05feat07 | #203 | PLANNER role: propose the next issues from the goal, wiki and latest metrics | M05feat05 | factory + human |
| M05feat08 | #204 | Conductor goal loop: re-measure after merge, plan more work until met or stopped | M05feat06, M05feat07 | factory |

### M06: Hardening and pilot

| Key | Issue | Title | Blocked by | Owner |
|---|---|---|---|---|
| M06feat01 | #112 | Treat client content as untrusted input | M05feat02 | factory |
| M06feat02 | #109 | Instance lifecycle: GC and stale-session reaper | M04feat02 | factory |
| M06feat03 | #111 | Per-role tracing | M05feat01 | factory |
| M06feat04 | #138 | Pilot: ollama-monitor in shadow mode | M01feat04, M05feat02, M05feat04, M06feat01, M05feat08 | factory + human |
| M06feat05 | #167 | Pilot: ollama-monitor in recommend mode, end to end | M06feat04 | factory + human |
| M06feat06 | #133 | Remove the legacy single-agent path | M06feat05 | factory |

### M07: Backlog after the pilot

| Key | Issue | Title | Blocked by | Owner |
|---|---|---|---|---|
| M07feat01 | #145 | Client CI auth via GitHub OIDC instead of ATOBAR_TENANT_TOKEN | M06feat05 | factory |
| M07feat02 | #168 | LLM adapters for Claude, DeepSeek and Grok | M06feat05 | factory |
| M07feat03 | #158 | Control Panel: hardened container build with strict security scanners | M06feat05 | factory |
| M07feat04 | #115 | Wiki generation refactor (tenant-scoped, read-only DoD exception) | M06feat05 | factory |
| M07feat05 | #117 | uv standardisation (single dependency source, hermetic installs, client contract) | M06feat05 | factory |
| M07feat06 | #169 | Control Panel: loop history views (rounds, cost, escalations) | M06feat04 | factory |
| M07feat07 | #172 | Flexible evaluation spec table normalization and localized column aliases | M06feat05 | factory |
| M07feat08 | - | Granular BYOK key management: per-invoking-user key routing & cost sharing on top of per-repo, per-role routing | M09feat08 | factory |

### M09: Agent runtimes

| Key | Issue | Title | Blocked by | Owner |
|---|---|---|---|---|
| M09feat01 | #241 | J23 Extract spot-instance agent loop behind pluggable AgentRuntime seam | - | factory |
| M09feat02 | #242 | J24 Expose flow tools over stdio MCP server | - | factory |
| M09feat03 | #243 | J25 NanobotRuntime: run swarm roles through nanobot Python SDK | #241, #242 | factory |
| M09feat04 | #244 | J26 Route role runners through runtime registry + agnostic LLM key delivery | #241, #243 | factory |
| M09feat05 | #245 | J27 Plumb agent_runtime end-to-end: manifest knob, conductor, dispatcher, cloud-init | #244 | factory |
| M09feat06 | #246 | J28 EVALUATOR on nanobot: visual tools via MCP and verdict path | #243, #244 | factory |
| M09feat07 | #247 | J29 E2E pilot: nanobot runtime on ollama-monitor (shadow) + rollback proof | #245, #246 | factory + human |
| M09feat08 | #275 | NanoBot pivot final pass: cleanup + multi-provider LLM key fan-out | #243, #244 | factory |

### M10: Maintenance & Diagnostic Skills (Done)

| Key | Issue | Title | Blocked by | Owner |
|---|---|---|---|---|
| M10feat01 | #167 | Consolidated `atobar-ops` CLI & `.pi/skills/atobar-ops` replacing standalone scripts | - | factory |

### M11: Telemetry overhaul

| Key | Issue | Title | Blocked by | Owner |
|---|---|---|---|---|
| M11feat01 | #283 | Telemetry overhaul: collapse to one run_events stream, fix the pipes, delete dead telemetry | - | factory |


## Picking up an issue (30-40 minute agent pass)

1. Read root `AGENTS.md` (auto-loaded by `pi`, Claude, and Devin), then the issue. Use `rg -n` to query specific sections of `docs/*.md` only as needed.
2. Check every **Blocked by** issue is closed (`gh issue view <n> --json state`). If not, stop.
3. Set up: `uv venv -q .venv && . .venv/bin/activate && uv pip install -q -e . -r requirements-dev.txt pytest pytest-asyncio`.
4. Branch `flow-<issue>-<slug>`. Write the acceptance-criteria tests first, then the code, touching only the files in **Scope**.
5. Verify: `uv run --no-sync atobar-ops verify`
   (and `actionlint` / `shellcheck` for workflows and scripts).
6. Open a PR to `main` (no stacked PRs) with the sections required by `AGENTS.md` and `Closes #<issue>`. Never merge.
7. If something in the issue is wrong or missing, say so in the PR and comment on the issue; do not widen scope.

Agents need no cloud credentials for any M01–M03 issue: tests use fakes (`control-panel/tests` shows the pattern of
overriding FastAPI dependencies). Human steps are labelled `owner:human` or `factory + human` in the issue.

## Grooming rules

- One issue = one PR = one 30-40 minute pass: at most ~3 source files plus their tests, one new concept.
- Title `M<xx>feat<yy> <imperative title>`; numbering follows the critical path inside a milestone.
- Body: key line, **Blocked by**, **Blocks**, Read first, Goal, Scope (exact paths), Out of scope, Acceptance criteria
  (tests first), Verify. Template: `.github/ISSUE_TEMPLATE/roadmap-task.md`.
- Too big? Split into `featNNa`/`featNNb` and fix the Blocked by lines of its dependants.
- New finding outside the current milestone? Open an issue in M07 (backlog) instead of growing the current one.
