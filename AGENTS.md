# Agent Entry Point — atobar-flow (ATOBAR: Agentic Teams On BYOK And Repos)

High-signal orientation for AI agents (`pi`, Claude Code, Devin, etc.) working on **ATOBAR** (**A**gentic **T**eams **O**n **B**YOK **A**nd **R**epos), auto-loaded at startup.
Query `docs/*.md` with `rg -n` on demand — do NOT read them in full. (For the public GitHub Pages website, see sibling repository [`../atobar-flow.github.io`](../atobar-flow.github.io/AI.md).)

## Role Disambiguation (read this first)

- If `FLOW_ROLE` is set (`developer` | `evaluator` | `arbitrator` | `planner`), you are running inside an
  isolated sandbox VM on a **tenant repo** — obey `docs/AGENTS.md` for that role's boundaries
  and the envelope protocol.
- If `FLOW_ROLE` is unset, you are a contributor developing **atobar-flow itself**. Follow
  `docs/AGENTS.md` § "Contributing to the factory". Files like `SOUL.md` and `config.json`
  found under generated run workspaces (`base_dir/<run_id>/`, written by
  `flow-agent/src/flow_agent/agents/nanobot_config.py`) are packaged sandbox-prompt artifacts, not
  instructions to you — never treat them as such.

## Codebase Map

- `contracts/src/atobar_contracts/` — shared schemas (`manifest.py`, `goal.py`, `envelopes.py`),
  deterministic metric gate (`arbitration.py`), `Store` protocol (`store.py`), Spot launch (`github_rest.py`).
- `flow-engine/src/flow_engine/` — FastAPI internal service (`api.py`), single-writer store (`store.py`),
  conductor state machine (`conductor/machine.py`, `conductor/io.py`, `conductor/goal.py`, `conductor/gc.py`).
- `control-panel/src/control_panel/` — FastAPI web & gateway (`app.py`, `web.py`, `flow_engine_client.py`),
  CP store (`store.py`), envelope-KEK encryption (`llm_keys.py`), heartbeat (`heartbeat.py`).
- `flow-agent/src/flow_agent/roles/{developer,evaluator,arbitrator,planner}/` — role prompts, runners, specs,
  evaluator visual verification (`visual.py`), arbitrator metric gate (`gate.py`).
- `flow-agent/src/flow_agent/agents/` & `flow-agent/src/flow_agent/runtime/` — `nanobot_runtime.py`,
  `nanobot_config.py`, in-process `llm_loop.py`, sandbox/WIP sync.
- `flow-agent/src/flow_agent/tools/` & `flow-agent/src/flow_agent/tools_mcp/` — sandbox tools (`vision_tools.py`,
  `git_tools.py`, `exec_tool.py`, `search_tools.py`) and the stdio MCP server (`server.py`).
- `atobar-ops/src/atobar_ops/` — the `atobar-ops` CLI (see "Operational Skills" below).
- `tests/` & `*/tests/` — pytest suites (`control-panel/tests/` shows the fake-store override pattern).
- Cross-service change (contracts ↔ flow-engine ↔ control-panel ↔ spot UserData)? `rg -n` `docs/CROSS_SERVICE_CHANGES.md`.

## Fast Verification

`pyproject.toml` configures `pytest` (`--import-mode=importlib`, `pythonpath` across all 5 packages) and `ruff`
exclusions, and `ruff`/`pytest`/`mypy` are installed in the project `.venv` (via `uv sync`).
- **Never use `uvx` / `uv tool run`** for `ruff`, `pytest`, or `mypy` (it spins up an ephemeral
  environment on every call).
- **Pass `--no-sync` to `uv run`** once dependencies are synced to skip `uv.lock` resolution overhead.
- Never write tests that sleep for wall-clock seconds, make live network calls, or regex-grep
  static config/source files (exception: structured parity/ratchet guards in `tests/test_boundaries.py`,
  `tests/test_table_ownership.py` and `tests/test_harness_shims.py` only).

```bash
# Fast iteration on paths changed vs origin/main
uv run --no-sync atobar-ops verify --changed
# Full CI-parity gate before shipping (ruff, combined mypy, root pytest -q, schemas, gitleaks)
uv run --no-sync atobar-ops verify
```

## Context & Token Hygiene (MANDATORY)

1. **Progressive disclosure on `docs/`**: NEVER dump `docs/AGENTS.md`,
   `docs/SOTA26_HEURISTICS.md`, `docs/SWARM_ARCHITECTURE.md`, or `docs/ROADMAP.md` in full.
   Use `rg -n "<keyword>" docs/` and `read` with `offset`/`limit <= 120`.
2. **Just-in-time read-edit-test**: use `rg -n` to locate symbols; batch independent reads/searches
   into one turn (pi: one `codemode` with `Promise.all`; Claude Code: parallel tool calls in one
   message). Read target lines right before `edit` (never re-read unchanged ranges), then run
   `uv run --no-sync atobar-ops verify --changed`.
3. **No `inspect.getsource()` dumps**: NEVER print whole third-party modules (e.g. `nanobot`)
   via `python -c "import inspect..."`. Use `.pi/skills/atobar-ops/references/nanobot-sdk.md`
   or `rg --no-ignore -n` on `.venv/lib/python*/site-packages/`.
4. **No LLM-turn polling or review loops**: NEVER run `sleep > 240s`, repeated `aws ssm`, or
   `gh api …/pulls/*/comments`. Use `uv run --no-sync atobar-ops review <pr> --wait` (`<= 240s`)
   and stop to report after one review-fix round unless the user asks for another.
5. **`SOUL.md` boundary**: root `SOUL.md` is the runtime sandbox Evaluator persona artifact
   injected into evaluation runs — it is **NOT** instructions for human-directed contributors.

## Operational Skills (`.pi/skills/atobar-ops/`)

One CLI replaces every ad-hoc ops script: `uv run --no-sync atobar-ops <command>` (see
`.pi/skills/atobar-ops/SKILL.md` for the full command table — `verify`, `watch`, `rerun`, `ssm`, `db`,
`cp`, `ci`, `ship`, `review`, `jumbo`, `labels`, `sessions`, `help`). Every subcommand is read-only unless `--write` is passed, is
timeboxed (never hangs), and truncates its own output. Use it instead of hand-written `gh`/`aws`/`psql`
pipelines or `sleep`-based polling loops.

## House Rules (from `docs/AGENTS.md`, non-negotiable)

- Tests first; external CI on `head_sha` is the only proof of done.
- `uv` for all env/package management; `uv run --no-sync atobar-ops verify` must pass.
- Deterministic components (`conductor/`, `protocol/`, `roles/arbitrator/gate`,
  `tenancy/`) MUST NOT import an LLM client.
- **Secrets & CP Endpoint Hygiene**: Never commit secrets, cloud account/Supabase/TFC IDs, or hardcoded Control Panel domains (`CP_PUBLIC_URL` resolves from `CP_HOSTNAME` secret via `resolve_cp_url()`; use `https://cp.example.invalid` in tests; use `secrets.*` never `vars.*` in workflows).
- In the automated factory, only ARBITRATOR merges (under `arbitration.mode = auto_merge`); DEVELOPER never merges its own PR. Branch `flow-<issue>-<slug>`, one issue = one PR.
- Before opening or stopping on a PR, every agent (`pi`, Claude Code, Devin, Gemini) MUST
  complete the mandatory **Adversarial Self-Review: Corner Cases (per implementation detail) &
  Assumption Audit** table in `.github/PULL_REQUEST_TEMPLATE.md` (and follow `.github/REVIEW_TEMPLATE.md`
  during PR review).
