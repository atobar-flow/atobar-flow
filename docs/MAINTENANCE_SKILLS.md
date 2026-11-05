# Maintenance & Diagnostic Skills

This plan (`monitor_ec2`, `query-supabase`, and friends as separate per-script skills, including
a short-lived parallel attempt under `.agents/skills/`) has been implemented and superseded by a
single consolidated skill and CLI:

- Skill: `.pi/skills/atobar-ops/SKILL.md`
- CLI: `uv run atobar-ops <command>` (package: `atobar-ops/src/atobar_ops/`)

See the skill file for the full command table (`watch`, `rerun`, `ssm`, `db`, `cp`, `ci`, `ship`,
`jumbo`). All legacy standalone scripts (`scripts/monitor_ec2.py`, `scripts/query_supabase.py`,
`scripts/e2e_pilot.sh`, `scripts/check_instances.sh`, `scripts/capture_x11.sh`,
`scripts/connect_client.sh`) have been removed in `#167`.
