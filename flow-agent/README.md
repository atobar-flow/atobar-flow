# flow-agent

## Purpose
Warm EC2 Spot Session Supervisor (`flow_agent.main`), role runners (`planner`, `developer`, `evaluator`, `arbitrator`), `nanobot` runtime integration, stdio MCP tool server (`flow_agent.tools_mcp.server`), X11/Chrome visual verification tools, and `SotaWiki` generator.

## Runtime
Executes on ephemeral Ubuntu EC2 Spot instances launched from the Packer AMI (`flow-sandbox-v*`) with isolated per-role `git worktree` checkouts and Xvfb display `:0`.

## APIs
- `python -m flow_agent.main`: Warm Spot Session Supervisor loop across `planner` -> `developer` -> `evaluator` -> `arbitrator`
- `python -m flow_agent.tools_mcp.server --role <role>`: Role-allowlisted stdio MCP tool server
- `python -m flow_agent.knowledge.onboard_repo`: Repository `SotaWiki` synthesizer

## Consumes
- `atobar_contracts` for envelopes, manifest, arbitration gate, and GitHub REST client
- Control Panel `/v1/runs/{run_id}/*` endpoints for single-role LLM key delivery, GitHub token minting, heartbeats, events, screenshots, and `turn-complete` warm handover
- LLM provider APIs (Gemini, Claude, DeepSeek, Grok) via `nanobot-ai` or in-process adapters

## Data
Writes local per-run workspace artifacts under `/home/ubuntu/flow-run/<run_id>/` and worktrees under `/home/ubuntu/worktrees/`. Never accesses Supabase directly (all persistence goes through Control Panel APIs).

## Credentials
Receives only short-lived `FLOW_RUN_ID` + `FLOW_RUN_CREDENTIAL` in user-data; fetches role-scoped LLM API key into memory via `POST /v1/runs/{id}/llm-key` and repo-scoped GitHub token via `POST /v1/runs/{id}/github-token`. Scrubs keys from RAM between warm role turns.

## Allowed imports
Stdlib, `atobar_contracts.*`, `flow_agent.*`, `nanobot`, `mcp`, `google.genai`, `httpx`, `pydantic`, `PIL`, `bs4`, `websockets`. Forbidden: `control_panel`, `flow_engine`, `atobar_ops`, `supabase`, `fastapi`.

## Develop & test
```bash
uv run --no-sync pytest flow-agent/tests -q
uv run --no-sync mypy flow-agent/src --ignore-missing-imports
```

## Troubleshooting
- **Checkout mismatch halt**: `FLOW_HEAD_SHA` did not match the checked-out commit in the role worktree.
- **Provider error halt**: Check `run_events` via `uv run atobar-ops db events <repo> --issue <N>` for classified provider error details.

## Future repo
`github.com/atobar-flow/flow-agent`
