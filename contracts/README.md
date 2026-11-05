# atobar-contracts

## Purpose
Pure deterministic schemas, protocol envelopes (`flow-ack`, `flow-verdict`, `flow-arbitration`, `flow-plan`), `TenantManifest`, `Store` protocol, state machine (`step()`), metric arbitration gate, and GitHub REST helpers shared across all atobar-flow microservices.

## Runtime
Library package (`atobar_contracts`) installed into every service runtime (Python 3.11+). Has no daemon or background process of its own.

## APIs
- `atobar_contracts.envelopes`: `Ack`, `Verdict`, `Arbitration`, `Plan`, `parse()`, `render()`, `compute_event_id()`
- `atobar_contracts.manifest` & `schema`: `TenantManifest`, `FlowMetricsReport`, `load_manifest()`, `check_schemas()`
- `atobar_contracts.machine` & `sufficiency`: `LoopState`, `step()`, `assess_issue_sufficiency()`
- `atobar_contracts.arbitration`: `Observation`, `Decision`, `arbitrate()`, `run_arbitrator()`
- `atobar_contracts.providers`: `canonical_provider()`, `PROVIDERS`
- `atobar_contracts.github_rest`: `GitHubClient`, `RealGitHubClient`

## Consumes
Only `pydantic` and `httpx`. Zero internal service dependencies.

## Data
Owns JSON Schema definitions under `contracts/schemas/` (`tenant.schema.json`, `flow-metrics.schema.json`). Reads no database tables directly.

## Credentials
None. Never holds or reads secrets, database keys, or LLM tokens; `run_credentials.py` only generates random token/hash pairs.

## Allowed imports
Stdlib, `pydantic`, `httpx`, and `atobar_contracts.*`. Forbidden: `control_panel`, `flow_engine`, `flow_agent`, `atobar_ops`, `supabase`, `fastapi`, and all LLM SDKs.

## Develop & test
```bash
uv run --no-sync pytest contracts/tests -q
uv run --no-sync mypy contracts/src --ignore-missing-imports
uv run --no-sync python -m atobar_contracts.schema --check
```

## Troubleshooting
- **Schema drift failure in CI**: Run `uv run --no-sync python -m atobar_contracts.schema` to regenerate `contracts/schemas/*.json`.
- **Boundary test failure**: Verify no service or LLM SDK was imported inside `contracts/src/atobar_contracts/`.

## Future repo
`github.com/atobar-flow/atobar-contracts`
