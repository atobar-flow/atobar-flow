# atobar-ops

## Purpose
Unified, timeboxed operations CLI (`uv run atobar-ops`) replacing ad-hoc `gh`, `aws`, `psql`, and `ssh` polling scripts for watching runs, resetting E2E issues, inspecting Spot instances via SSM, querying/migrating Supabase, and shipping PRs.

## Runtime
Operator & contributor CLI invoked locally or inside agent sessions (`atobar-ops` console script). Read-only by default unless `--write` is passed; every command enforces a strict wall-clock timeout and output budget.

## APIs
- `atobar-ops watch <repo> <issue>`: Single-call bounded poll until state change or terminal outcome
- `atobar-ops rerun <repo> <issue> [--write]`: Reset issue comments, PRs, branches, labels, and DB rows
- `atobar-ops ssm <repo> <issue> <recipe>`: Execute `agent-log`, `cloud-init`, `git-rev`, or `display` over AWS SSM
- `atobar-ops db {tenant,set-model,events,sql,prune,migrate}`: Read-only diagnostics and gated DB mutations
- `atobar-ops cp {status,probe,keycheck}`: Control Panel health, auth surface probe, and provider key validation
- `atobar-ops ci <repo> [run_id]` & `atobar-ops ship --issue <N> [--merge]` & `atobar-ops jumbo new`

## Consumes
- `atobar_contracts.providers` for provider canonicalization and key validation
- Supabase REST & pooler (`psql`), GitHub CLI (`gh`), AWS CLI (`aws`), and SSH to Control Panel host

## Data
Stores local watch bookmarks in `.atobar/ops-state/` (gitignored).

## Credentials
Resolves credentials from environment variables, local `.env`, or AWS Secrets Manager (`flow/supabase/credentials`, `flow/control-panel/llm_kek`), automatically redacting secrets from all stdout/JSON output.

## Allowed imports
Stdlib, `atobar_contracts.*`, `atobar_ops.*`, `supabase`, `cryptography`, `httpx`. Forbidden: `control_panel`, `flow_engine`, `flow_agent`.

## Develop & test
```bash
uv run --no-sync pytest atobar-ops/tests -q
uv run --no-sync mypy atobar-ops/src --ignore-missing-imports
```

## Troubleshooting
- **EXIT_TIMEOUT (code 2)**: Command hit `--timeout` budget before terminal state; re-run `atobar-ops watch` to continue from bookmark.
- **Missing Supabase credentials**: Export `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` or configure AWS CLI profile with access to `flow/supabase/credentials`.

## Future repo
`github.com/atobar-flow/atobar-ops`
