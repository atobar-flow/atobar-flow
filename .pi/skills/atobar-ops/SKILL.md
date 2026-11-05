---
name: atobar-ops
description: "Run atobar-flow operations via the `atobar-ops` CLI: verify code, ship PRs, inspect or wait on PR reviews, watch or rerun E2E tenant issues, read EC2 logs over SSM, query/introspect/migrate Supabase (`db`, `db schema`), check Control Panel (`cp`), inspect CI minutes, or manage Jumbo issues, labels, and session audits."
---

# atobar-ops

One CLI (`uv run --no-sync atobar-ops <command>`) for all repository, E2E tenant, Supabase, Control Panel, and CI operations. Every subcommand is read-only unless `--write` is passed, enforces a bounded `--timeout` (default `<= 240s`), and budgets its own output (`--verbose` / `--max-chars` / `--json`).

## Commands

<!-- BEGIN GENERATED COMMAND TABLE -->
| Command | Use it to |
|---|---|
| `watch <owner/repo> <issue> [--until terminal\|change]` | Poll telemetry, run_events, loop_rounds, EC2 state, and PRs until change or terminal state |
| `rerun <owner/repo> <issue> [--write] [--no-relabel] [--keep-pr N] [--label LABEL]` | Reset an issue (comments, flow* PRs, flow-wip/* branches, DB rows, labels); dry-run without --write |
| `ssm <owner/repo> <issue> [--role planner\|developer\|evaluator\|arbitrator] [<recipe>]` | Run a bounded recipe (agent-log, cloud-init, git-rev, display) or `-- <cmd>` on active EC2 via SSM |
| `db tenant <owner/repo>` | Tenant row, manifest_sha, active metric count, role->model config (incl. planner), and BYOK key status |
| `db set-model <owner/repo> <role> <provider> <model> [effort]` | Configure a role's LLM provider, model, and reasoning effort |
| `db events [<owner/repo>] [--issue N] [--run R] [--limit N]` | Inspect run_events, loop_rounds, and arbitration_decisions for a run or issue |
| `db schema [table]` | List public tables with row estimates, or columns (name, type, nullable) for one table |
| `db sql <SELECT ...>` | Read-only SELECT/WITH query via Supabase pooler (with column/table suggestions on 42P01/42703) |
| `db prune <owner/repo> --issue N [--write]` | Delete loop_rounds and arbitration_decisions rows for one issue (dry-run without --write) |
| `db migrate <file> [--write]` | Apply an idempotent SQL migration file via pooler (dry-run without --write) |
| `cp status` | Control Panel deployed SHA vs origin/main, refresh timer, journal, and docker status |
| `cp probe [--repo owner/repo]` | Probe Control Panel routes and verify screenshot routes require authentication |
| `cp keycheck <provider>` | Validate an LLM provider key read from the KEY env var (never pass keys on the CLI) |
| `ci <owner/repo> [run_id] [--limit N] [--workflow W] [--minutes]` | Recent workflow runs + failed job logs; `--minutes` summarizes billed Actions minutes and 60s-cliff risk |
| `verify [--changed]` | CI-parity workspace verification (ruff, combined mypy, pytest --tb=short, schemas, gitleaks) |
| `ship [--issue N] [--title TITLE] [--body TEXT] [--body-file PATH\|-] [--merge]` | Full verify, push, open/update PR (Fixes/Refs #N + audit table), wait on CI run only (optional --merge) |
| `review <pr> [--repo owner/repo] [--unresolved] [--wait] [--until-reviewer SUBSTR]` | Fetch PR reviews, inline threads, and review-round counter; `--wait` polls <=240s for new reviews |
| `jumbo new <NN\|auto> <slug> --title TITLE [--context TEXT] [--from-file PATH\|-]` | Create `issues/J<NN>-<slug>.md`, open GitHub issue, update `JUMBO_STATUS.md` (`auto` number, `-` stdin) |
| `jumbo sync <NN>` | Push `issues/J<NN>-*.md` to the matching GitHub issue body (`gh issue edit --body-file`) |
| `labels <owner/repo> [--write]` | Seed canonical Atobar Flow labels on a tenant repo (dry-run without --write) |
| `sessions audit [--harness pi\|claude\|all] [--since DATE] [--dir PATH]` | Summarize turns, tokens, cost, cold-cache turns, repeat reads, sleeps >240s, and SQL/ship errors |
| `init-app [--org ORG] [--slug SLUG] [--cp-url URL] [--env-file PATH] [--force] [--dry-run]` | Verify GitHub App slug release, run GitHub App Manifest flow, and write credentials to .env |
| `infra sync-vars [--write] [--env-file PATH]` | Sync .env variables to HCP Terraform varset/workspaces and GitHub Actions (dry-run without --write) |
| `infra up [--write] [--env-file PATH]` | Apply all 4 Terraform workspaces in dependency order and output server_ipv4 (dry-run without --write) |
| `infra destroy [--write] [--env-file PATH]` | Destroy all 4 Terraform workspaces in reverse dependency order and purge secrets (dry-run without --write) |
| `help [--markdown]` | Render the CLI Markdown command table generated from build_parser() |
<!-- END GENERATED COMMAND TABLE -->

## Rules

- Preconditions and side effects for each command: `uv run --no-sync atobar-ops <cmd> --help`.
- Pass `--write` (on `rerun`, `db prune`, `db migrate`, `labels`) only when the user explicitly asked for that mutation.
- `ollama-monitor` is the designated E2E test tenant (issues #33 and #43 run the full developer->evaluator->arbitrator loop).
- `cp keycheck <provider>` reads its key from the `KEY` env var only; never pass a key as a CLI argument.
- Never hardcode live Control Panel domains, AWS account IDs, Supabase refs, or TFC IDs in code, docs, or issues; `CP_PUBLIC_URL` is composed from the `CP_HOSTNAME` secret (`infra sync-vars --write` syncs GitHub Actions `secrets.*` and sensitive TFC vars).

## Which reference to read

- `references/e2e-runbook.md` — E2E lifecycle, envelopes (`flow-ack`/`flow-verdict`/`flow-arbitration`), telemetry status strings, common halt causes.
- `references/control-panel.md` — Hetzner host layout, refresh timer, and route/key verification.
- `references/nanobot-sdk.md` — `nanobot` SDK file map and class signatures.
