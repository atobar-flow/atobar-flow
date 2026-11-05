# E2E Run Lifecycle Reference

Full step-by-step runbook: `docs/E2E_RUNBOOK.md`. This is the compact version for diagnosing a run
with `atobar-ops watch`/`db events`/`ssm`.

## Lifecycle

1. Label `flow:feature` applied to a tenant issue → the tenant's `flow-trigger.yml` posts to the
   Control Panel event bus.
2. Conductor dispatches the **developer** role: provisions an EC2 spot instance tagged
   `flow-developer-{owner}__{repo}-{issue}`, runs the agent loop in the tenant's worktree.
3. Developer pushes a branch (`flow/issue-<N>`), opens a PR, posts a `flow-ack` envelope (fenced
   ` ```flow-ack ``` ` on the PR), then stops.
4. Conductor dispatches the **evaluator** (`flow-evaluator-...`) for that exact `head_sha`. It posts
   a `flow-verdict` envelope (`approved` or `changes_requested`) and sets the `flow/eval` check.
5. On `changes_requested`, conductor re-dispatches the developer with the verdict as new input
   (next `loop_rounds.round`). On `approved` + green checks, conductor dispatches the
   **arbitrator**.
6. Arbitrator evaluates the tenant's metric gate and posts a `flow-arbitration` envelope
   (`decision`: `pass` | `fail` | `blocked_requires_human`; `mode`: `shadow` | `recommend` |
   `auto_merge`). Only `auto_merge` actually merges the PR — `shadow` and `recommend` record the
   decision and leave the PR open.
7. Each role phase posts/updates one GitHub issue comment starting `### 🌊 flow Agent Telemetry
   (Live)` with a `*Status: ... | Time Elapsed: ...*` line. `atobar-ops watch` parses the latest
   one of these as the one-line status.

## Status strings

- `🟢 Running` — a role is actively looping.
- `✅ Complete` — that role's phase finished (not necessarily the whole run — each role posts its
  own telemetry comment, so "Complete" can mean "developer done, evaluator not yet dispatched").
- `🔴 Halted: <reason>` — the role hit an unhandled exception or a circuit breaker; check
  `ssm <repo> <issue> agent-log` and `db events` for the last events before `lifecycle.end`.

## `run_events.kind` values worth knowing

`conductor.dispatch`, `lifecycle.start`, `model_call`, `tool_call`, `llm_key`, `envelope`,
`verdict`, `lifecycle.end`. A `lifecycle.end` payload has `error: true/false` and `reason`.

## Common halt causes

- **Required check never went green** — evaluator can't converge; check `ci <tenant repo>`.
- **Circuit breaker tripped** (too many rework rounds) — conductor posts `flow-escalation` and
  stops dispatching; needs a human (or `rerun --write` to start clean).
- **LLM key missing/invalid for a role** — run fails fast with a 409 from
  `/v1/runs/{run_id}/llm-key`; check `db tenant <repo>` for `<UNCONFIGURED>` roles or a failed
  `cp keycheck`.
- **Instance never came up** — `watch` shows no EC2 instance after a dispatch event; check
  `ci atobar-flow/atobar-flow` for a failed `flow_dispatcher`/`flow_conductor` run.

## `loop_rounds` / `run_events` gotcha

`loop_rounds.pr_number` (and the `arbitration_decisions` table) is keyed by the conductor's
dispatch id, which is the GitHub **issue** number passed as `FLOW_PR`/`ISSUE_ID` — not the number
of the PR the developer eventually opens. `db events --issue N` and `rerun`/`db prune` filter on
`N` itself, not on a linked PR's number.
