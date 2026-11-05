# J37 (atobar-ops): atobar-ops: unified operations skill & CLI

## Goal
Replace the per-script ops skills (`atobar-issue-reset`, `atobar-supabase-diag`, `atobar-ec2-monitor`,
`atobar-nanobot-inspect`) and ad-hoc `sleep`+`gh`/`aws`/`psql` polling with one skill
(`.pi/skills/atobar-ops/`) backed by one CLI (`src/atobar_ops/`, `uv run atobar-ops`).

## Context
Agents were burning dozens of turns per E2E check on hand-written `sleep`+`gh`/`aws`/`psql` polling
loops and inline `python -c` Supabase/AWS scripts (see `docs/MAINTENANCE_SKILLS.md`'s original
per-script-skill plan, and a week of Pi/Gemini sessions: ~70 turns polling one E2E run, 171
hand-written `aws ssm` calls in another, 66 copies of the same `aws secretsmanager` fetch). This
consolidates them into bounded, read-only-by-default subcommands: `watch`, `rerun`, `ssm`, `db`,
`cp`, `ci`, `ship`, `jumbo`.

## Work Packages
- [x] WP-1: `src/atobar_ops/` package — `creds.py`, `output.py`, `timebox.py`, `resolve.py`,
      `state.py`, `db.py`, `gh.py`, `cli.py`; `atobar-ops` console script in `pyproject.toml`.
- [x] WP-2: `.pi/skills/atobar-ops/SKILL.md` + `references/` (e2e-runbook, control-panel,
      nanobot-sdk); `.claude/skills/atobar-ops` symlink; old `.pi/skills/atobar-{issue-reset,
      supabase-diag,ec2-monitor,nanobot-inspect}` removed; `.pi/settings.json` no longer loads the
      factory-only `skills/` directory.
- [x] WP-3: `scripts/monitor_ec2.py` and `scripts/query_supabase.py` turned into deprecation shims;
      `AGENTS.md`, `docs/E2E_RUNBOOK.md`, `docs/MAINTENANCE_SKILLS.md` updated to point at
      `atobar-ops`.
- [x] WP-4: Unit tests (`tests/atobar_ops/`, fakes/monkeypatch, no network) + live read-only smoke
      test against real AWS/Supabase/Control Panel + live E2E rerun on
      `arsenyspb/ollama-monitor#43` + cross-harness skill-trigger checks (Claude Code, Pi).

## Acceptance
- [x] `PYTHONPATH=src:tests/control_panel uv run --extra agents pytest tests/atobar_ops -q` and the
      full suite pass; `ruff check src/ tests/` and `mypy src/` clean.
- [x] Live read-only smoke (`db tenant`, `db sql`, `cp status`, `cp probe`, `ci`, `watch`, `ssm`)
      each complete under their timeout with bounded output.
- [x] Live E2E: `rerun arsenyspb/ollama-monitor 43 --write` re-triggers the loop; `watch` alone
      (no raw `gh`/`aws`/`sleep`) tracks it to a terminal state.
- [x] Claude Code and Pi both discover and use the skill for operational prompts, and do not use it
      for an unrelated code-reading prompt.

## Risks
- `loop_rounds`/`arbitration_decisions.pr_number` is actually the conductor's dispatch id (the
  GitHub *issue* number), not the eventual PR's number — `db events`/`rerun`/`db prune` filter on
  the issue number for this reason; a future conductor change renumbering dispatch ids would need a
  matching update here.
- `cp status`'s Terraform-drift check is best-effort (docker/journal output only, no automated
  diff against `infra/terraform/control-panel-hetzner/`).
