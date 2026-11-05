# flow-engine

## Purpose
Deterministic orchestration engine: Conductor event processing (`conductor/io.py`), EC2 Spot sandbox dispatch (`conductor/dispatch.py`), stale-instance/session garbage collection (`conductor/gc.py`), and tenant manifest onboarding (`tenancy/`).

## Runtime
Invoked by GitHub Actions workflows (`flow_conductor.yml`, `flow_dispatcher.yml`, `flow_gc.yml`, `onboard-tenant.yml`) and transitioning in SEQ-03 (#301) to an internal FastAPI service (`:8081`) + `janitor` worker.

## APIs
- `python -m flow_engine.conductor.io`: Process a `flow_event` payload deterministically through `step()`
- `python -m flow_engine.conductor.dispatch`: Launch a role-tagged EC2 Spot instance from `flow-spot-sandbox-template`
- `python -m flow_engine.conductor.gc`: Terminate timed-out/closed-PR Spot instances and reap stale sessions
- `python -m flow_engine.tenancy.{onboard,onboard_spec,spec,manifest,schema}`: Tenant onboarding & manifest validation CLIs

## Consumes
- `atobar_contracts` for state machine, envelopes, sufficiency gate, arbitration, and GitHub REST client
- AWS EC2 API (`boto3`) for Spot instance lifecycle
- Supabase for conductor event idempotency, `loop_rounds`, `run_credentials`, and `sessions`

## Data
Reads/writes `conductor_events`, `loop_rounds`, `run_credentials`, `sessions`, `tenants`, and `metric_definitions`.

## Credentials
`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `GITHUB_APP_ID` / `GITHUB_APP_PRIVATE_KEY` (or `GITHUB_TOKEN` installation token), and host AWS credentials (`flow-control-panel-hetzner` via `aws.env`) for EC2 Spot dispatch and GC. (The GitHub Actions OIDC role `flow-gha-dispatcher-role` is scoped exclusively to `.github/workflows/supabase-migrate.yml` on `main`.)

## Allowed imports
Stdlib, `atobar_contracts.*`, `flow_engine.*`, `boto3`, `supabase`, `httpx`, `pydantic`. Forbidden: `control_panel`, `flow_agent`, `atobar_ops`, and all LLM SDKs (`google.genai`, `nanobot`, `openai`, `anthropic`).

## Develop & test
```bash
uv run --no-sync pytest flow-engine/tests -q
uv run --no-sync mypy flow-engine/src --ignore-missing-imports
```

## Troubleshooting
- **Duplicate event skipped**: `conductor_events` already recorded `event_id` or `comment:<pr>:<id>`.
- **Spot dispatch failure**: Check AWS Spot capacity in `us-west-2` and launch template `flow-spot-sandbox-template`.

## Future repo
`github.com/atobar-flow/flow-engine`
