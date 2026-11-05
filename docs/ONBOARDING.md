# Onboarding a Client Repo (tenant)

This is the maintainer's copy of the onboarding guide. Repository admins read the public version at
`<CP_PUBLIC_URL>/onboarding` (`ONBOARDING_GUIDE` in `control-panel/web.py`); keep both in sync.

atobar-flow runs everything remotely. An admin's laptop is used once, to connect the repository, with the
script in the public repo [`scripts/onboard.sh`](https://github.com/atobar-flow/atobar-flow/blob/main/scripts/onboard.sh) (that repo holds
only `onboard.sh`, its tests and its README):

```bash
curl -fsSL https://raw.githubusercontent.com/atobar-flow/atobar-flow/main/scripts/onboard.sh | bash
```

## Part A: One-Time Repository Setup (~5 min)

| # | Stage | Who | Mechanism | Done when |
|---|---|---|---|---|
| 1 | Install the `atobar-flow-agent` GitHub App on the repo | Repo admin | GitHub | Repo listed under *Add a repository* in the Control Panel |
| 2 | Register the tenant and generate its token | Repo admin | Control Panel `POST /tenants`, `POST /tenants/{repo}/token` | `tenants.status = onboarding`, `arbitration_mode = shadow`, canonical `flow:*` labels seeded via GitHub App token |
| 3 | Configure LLM key, role models, and Human Assignee | Repo admin | Control Panel `/tenants/<owner>/<repo>` (`LLM keys`, `Model per role`, `Human Assignee & Reviewer`). Use a dedicated API key for atobar-flow with a provider-side spend limit; the key is used inside the sandbox VM. | BYOK key validated, models chosen for `developer`/`evaluator` (`planner` falls back to `developer`), and assignable human collaborator set (defaults to onboarding admin) |
| 4 | Run `onboard.sh` in a clone and merge the trigger PR | Repo admin | `atobar-init`; saves `ATOBAR_TENANT_TOKEN` secret, synthesizes the initial `SotaWiki` (with a validated Mermaid architecture diagram, `J50`), and opens the trigger PR with `.github/workflows/flow-trigger.yml` | Trigger workflow is on the default branch and `SotaWiki` is visible on the Control Panel repo screen |

## Part B: Metric Calibration & Go-Live (First CI Run on `main`)

| # | Stage | Who | Mechanism | Done when |
|---|---|---|---|---|
| 5 | Author, compile, and approve at least 1 metric | Repo admin | Control Panel *Repository Metrics* section (`Draft → Compile → Approve`) | Metric definition active; `flow-metrics.yml` PR opened in client repo |
| 6 | Merge the `flow-metrics.yml` PR | Repo admin | Client repo | Default-branch CI runs `flow-metrics.yml` and reports observations to `POST /v1/metrics` |
| 7 | Automatic activation (`ready_to_activate`) | atobar-flow | Once all active metrics have reported `>= min_samples` default-branch observations | `tenants.status = active`; work labels (`flow:feature`, `flow:fix`, `flow:plan`) now dispatch agent runs |
| 8 | Graduate arbitration: `shadow` → `recommend` → `auto_merge` | Repo admin | Control Panel *Arbitration mode* | In `shadow`/`recommend`, passing PRs request a review from your Human Assignee; in `auto_merge`, passing PRs merge automatically |

**Empty repositories.** A repository with no commits can be onboarded so atobar-flow builds it from zero. In stage 4
`onboard.sh` pushes the first commit (README and trigger workflow) to the default branch instead of opening a PR,
and the spec issue gets a *Build from zero* section (language, layout, entry points, first user-visible milestone).
If the clone has commits that were never pushed, the script stops and asks to push them first.

Label rules enforced by `POST /v1/events`:

- **Human labels (5):** `flow:feature`, `flow:fix`, `flow:plan` (start work); `flow:approved` (approve a `flow:needs-approval` plan and start `developer`), `flow:resume` (reset circuit-breaker epoch and resume `developer`). Consumed command labels (`flow:approved`, `flow:resume`) are automatically removed so they can be used again on later rounds.
- **System state labels (5):** `flow:in-progress`, `flow:in-review`, `flow:needs-approval` (assigns Human Assignee), `flow:escalated` (assigns Human Assignee), `flow:shipped` (set on the delegated issue when a factory PR merges).
- **Control labels (2):** `flow:onboard` and `flow:metrics` never dispatch developer runs.
- **Conversational steering (`@atobar-flow-agent`, `J49`):** Collaborators (`OWNER`, `MEMBER`, `COLLABORATOR`, or the configured Human Assignee) can also steer work in plain English without changing labels:
  - Comment `@atobar-flow-agent <instructions>` on an **Issue** to refine the `flow-plan` checklist, or `@atobar-flow-agent approve` to start `developer`.
  - Comment `@atobar-flow-agent <instructions>` on a **Pull Request** to send fix instructions to `developer`, or `@atobar-flow-agent evaluate` to re-run `evaluator` on `head_sha` (automatically minting a fresh loop epoch if the PR was `flow:escalated`).
- **Living Architecture Wiki (`SotaWiki`, `J50`):** Every merged factory PR increments `merges_since_refresh` on `repository_wikis`. Once it reaches `refresh_interval_merges` (default 5), `flow-engine` queues a coalesced `onboarder` run (or you can click **Force Regenerate** in the Control Panel) to update the repo's Mermaid architecture diagram.
- While a tenant is still `onboarding`, premature work labels are removed and a single deduplicated comment links to the Control Panel readiness checklist so the label can be re-applied once `active`.
- `paused` tenants get `409`.

## Writing an evaluation spec

The spec states, in numbers, what "better" means for the tenant's business. It is human intent; the binding
policy is the manifest in this repo, so a DEVELOPER agent can never weaken its own pass criteria.

| Manifest field | Spec column | Rule |
|---|---|---|
| `key` | key | `^[a-z][a-z0-9_]*$`, unique |
| `run` | measurement | Shell command run in the client's CI; its last stdout line must be a finite number |
| `comparator` | comparator | `>=`, `<=`, `>`, `<`, `==` |
| `threshold` | threshold | number |
| `baseline_mode` | baseline_mode | `absolute` or `no_regression` |
| `min_samples` | min_samples | integer ≥ 1 |
| `metrics_runtime` | metrics runtime | Python version and setup commands |

Guidance:

- Measure outcomes a user would notice, not implementation details. Prefer one or two strong metrics.
- Write down how each metric could be gamed. Agent PRs that touch `.github/workflows/flow-metrics.yml` or
  `.atobar/metrics/` are routed to a human, and their observations are not trusted for that PR.
- Every observation carries `commit_sha`, `workflow_path`, `run_id`, `run_attempt` and an artifact hash, and
  `metric_observations` is append-only.
- Start in `shadow`; graduate only after the metrics have proven to track the objective.

Example (pilot `arsenyspb/ollama-monitor`): the objective is non-intrusive access, i.e. the monitor reads
Ollama without a proxy in front of it. A metric such as `proxyless_access_ratio >= 1.0` (`absolute`) would
check that every monitored signal is collected directly from Ollama's own API.

### Target state (what the swarm keeps working toward)

Configure **Target State & Goal Budget** in the Control Panel (`tenant_goals`, `M05feat05-08` / `J38`) so atobar-flow keeps planning and shipping work until the service reaches its objective:

- **Objective:** one paragraph on what the service must become.
- **Targets:** metrics with the value to reach (`kind = target`). PRs pass a target by not moving away from it.
- **Guardrails:** metrics that must hold on every PR (`kind = guardrail`, `absolute` or `no_regression`).
- **Budget:** total cost, number of PRs and days for the goal; and how many merges without progress mean "stalled".

After each merge, `flow-engine/src/flow_engine/conductor/goal.py` (`goal_step()`) re-measures the default branch. If targets are unmet, a `PLANNER` agent opens the next issues (`flow:needs-approval`) until the targets are met or the budget/stall limit stops it and escalates to a human.

## Clean Sunset / Exit (Zero Lock-In)

Client repositories never import `atobar-flow` packages at runtime. Deleting `.github/workflows/flow-trigger.yml` and `.github/workflows/flow-metrics.yml` (and revoking the tenant token in the Control Panel) detaches a repository completely in one commit.
