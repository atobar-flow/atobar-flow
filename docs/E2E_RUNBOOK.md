# End-to-End Loop Validation Runbook

This runbook documents the deterministic steps and `atobar-ops` commands to verify that the
atobar-flow multi-agent PR loop operates end-to-end on a real tenant (`arsenyspb/ollama-monitor`)
across `shadow` and `recommend` arbitration modes.

## Pre-Flight Verification (`atobar-ops`)

Never run `sleep` polling loops or ad-hoc `aws`/`gh`/`psql` scripts. Use `uv run --no-sync atobar-ops`
(see `.pi/skills/atobar-ops/SKILL.md`); every call is bounded (`<= 240s`).

> **Pre-requisite (`~/.ssh/cp.key`)**: `atobar-ops cp status` connects over SSH using `~/.ssh/cp.key`, which must match the `ED25519` keypair provisioned by Terraform (`infra/terraform/control-panel-hetzner/main.tf` `hcloud_ssh_key.cp`) and stored in AWS Secrets Manager (`flow/control-panel/ssh_private_key`):
> `(umask 077 && aws secretsmanager get-secret-value --secret-id flow/control-panel/ssh_private_key --region "${AWS_REGION:-us-west-2}" --query SecretString --output text > ~/.ssh/cp.key)`

```bash
# 1. Verify Control Panel & Flow Engine deployment parity and containers
uv run --no-sync atobar-ops cp status

# 2. Verify zero-trust public surface (/healthz scrubbed, /docs & /openapi.json 404, screenshots 401)
uv run --no-sync atobar-ops cp probe --repo arsenyspb/ollama-monitor

# 3. Verify tenant status, arbitration_mode, role->model bindings, and KMS-envelope BYOK keys
uv run --no-sync atobar-ops db tenant arsenyspb/ollama-monitor
```

Ensure:
1. `deployed_sha == origin/main` and `drift=NONE`, with `control-panel-api-1`,
   `control-panel-flow-engine-1`, `control-panel-janitor-1`, and `control-panel-caddy-1` running.
2. `cp probe` returns `200 /healthz {"status":"ok"}`, `404 /docs`, `404 /openapi.json`, and
   `401` on unauthenticated screenshot routes.
3. `db tenant` shows `status=active`, `developer` and `evaluator` role models configured, and
   `decrypt=ok` on the tenant's BYOK key(s).

## Step 1: Triggering or Re-Running an Issue (`rerun` + `watch`)

To reset a pilot issue (closing stale `flow*` PRs, deleting orphan branches, pruning prior
`loop_rounds` / `arbitration_decisions` rows for that issue, and re-applying `flow:feature`):

```bash
# Dry-run plan (read-only)
uv run --no-sync atobar-ops rerun arsenyspb/ollama-monitor 43 --label flow:feature

# Execute reset + trigger + initial watch
uv run --no-sync atobar-ops rerun arsenyspb/ollama-monitor 43 --write --label flow:feature

# Watch until terminal state (converged/merged or halted)
uv run --no-sync atobar-ops watch arsenyspb/ollama-monitor 43 --until terminal --timeout 240
```

## Step 2: Warm Spot Supervisor Trajectory (`nanobot` Runtime)

1. `flow-trigger.yml` in the tenant repo forwards `issues.labeled` to `POST /v1/events` on the
   Control Panel, which proxies to `flow-engine` (`POST /internal/v1/events`) with `X-Request-ID`.
2. `flow-engine` evaluates deterministic spec sufficiency (`assess_issue_sufficiency`) and launches
   one EC2 Spot instance with `FLOW_AGENT_RUNTIME="nanobot"` and a single-use run credential.
3. The Spot instance boots without any AWS SSM/Secrets Manager secrets, mints scoped GitHub tokens
   via `POST /v1/runs/{run_id}/github-token`, and fetches its role-scoped LLM key in memory via
   `POST /v1/runs/{run_id}/llm-key`.
4. As each role (`planner` → `developer` → `evaluator` → `arbitrator`) completes its turn,
   `flow-agent` calls `POST /v1/runs/{run_id}/turn-complete` to claim the next role on the **same**
   warm Spot VM in `<5s`, isolating each role in its own git worktree under `/home/ubuntu/worktrees/`
   and scrubbing `FLOW_LLM_API_KEY*` between turns.

To inspect the live Spot instance over SSM while a run is active:

```bash
# Tail agent log
uv run --no-sync atobar-ops ssm arsenyspb/ollama-monitor 43 agent-log

# Verify zero-trust Spot environment (zero SUPABASE_*, GEMINI_API_KEY, or SSM private key access)
uv run --no-sync atobar-ops ssm arsenyspb/ollama-monitor 43 -- 'env | grep -E "SUPABASE_|GEMINI_API_KEY" || echo ZERO_SECRETS'
```

## Step 3: Telemetry, `loop_rounds` & `arbitration_decisions` Audit

Once `watch` reaches terminal state, inspect the persisted audit trail:

```bash
uv run --no-sync atobar-ops db events arsenyspb/ollama-monitor --issue 43
```

Verify:
- `RUN_EVENTS` contains `lifecycle.start`, `model_call`, `tool_call`, `verdict`, and `lifecycle.end`.
- `LOOP_ROUNDS` rows carry `runtime=nanobot`, non-zero `cost`, non-zero `wc` (`wall_clock_minutes`),
  and `calls > 0` (`usage_detail.calls` per-call token records).
- `ARBITRATION_DECISIONS` records the decision (`pass` / `fail` / `blocked_requires_human`), the
  active `mode` (`shadow` or `recommend`), and a non-null `manifest_sha`.

## Step 4: Circuit-Breaker & Provider-Failure Resilience

When a provider error (400/401/403/404/429) or circuit-breaker cap (`max_pr_wall_clock_minutes`,
`max_pr_cost_usd`, `max_review_rounds`) trips:
1. `NanobotRuntime` / `classify_provider_error` sanitizes the failure into a single-line
   `provider_error` / `provider_quota` / cap reason with zero raw stack traces.
2. The live GitHub issue telemetry comment transitions to `🔴 Halted: <reason>`.
3. `POST /v1/runs/{run_id}/heartbeat` (`status="Halted"`) records an idempotent `loop_rounds` row
   with partial `usage_detail` (`agent_runtime`, `total`, `calls`, `halt_reason`, `halt_detail`).
4. `_shutdown` executes `sudo shutdown -h now` so no orphan EC2 Spot instance remains running.

## Step 5: `shadow` vs. `recommend` vs. `auto_merge` Modes

- **`shadow`**: Evaluates `metric_definitions` / `manifest.metrics` against `metric_observations`
  from `flow-metrics.yml`, records the row in `arbitration_decisions` with `manifest_sha`, and posts
  the metric table on the issue (without creating `flow/arbiter` check runs or merging).
- **`recommend`**: Posts the metric table + `flow-arbitration` envelope on both the issue and PR
  (linking each metric to `https://$CP_HOSTNAME/tenants/<owner>/<repo>#metric-<key>` with baseline
  SHA) and creates the `flow/arbiter` GitHub Check Run (`success` / `failure`), leaving merge to a
  human.
- **`auto_merge`**: Creates the `flow/arbiter` check run and squash-merges the PR when all metrics
  and guardrails pass.

## Runtime & Provider Configuration

- `loop.agent_runtime` defaults to `"nanobot"` (running roles via the `nanobot-ai` SDK and stdio MCP
  server `flow_agent.tools_mcp.server`). `"native"` remains selectable in the schema as a fallback.
- Role-to-model bindings (`developer`, `evaluator`, `planner`, `arbitrator`) and KMS-envelope BYOK
  keys (`gemini`, `claude`, `deepseek`, `grok`, `openai`) are managed in the Control Panel at
  `https://$CP_HOSTNAME/tenants/<owner>/<repo>`.
