---
name: Roadmap task (M<xx>feat<yy>)
about: One 30-40 minute agent pass on the atobar-flow critical path (see docs/ROADMAP.md)
title: "M00feat00 <imperative title>"
labels: owner:factory
---
**M00feat00** · Milestone M00 (<name>) · Owner: factory · Epic #119
**Blocked by:** #<n> (M..feat..) or none
**Blocks:** #<n> (M..feat..) or none

## Read first
Read root `AGENTS.md` (auto-loaded by `pi`, Claude, and Devin) and use `rg -n` to query specific sections of `docs/*.md` only as needed for your issue.

## Goal
<one outcome, one or two sentences>

## Scope
- <exact files to add or change>

## Out of scope
<what the next issue does>

## Corner Cases & Simplification Audit
- **Corner Cases per Component (concurrency/races, partial failure & retry idempotency, pagination/bounds, auth/trust boundaries):**
  - `<component/file>`: <corner case + required deterministic handling>
- **Assumptions to Challenge & Simplifications:**
  - <what legacy shim, duck-typing fallback, or duplicate code path must be deleted or unified rather than worked around?>

## Acceptance criteria (tests first)
- [ ] AC-1:
- [ ] Workspace verification (`uv run --no-sync atobar-ops verify`) green

## Verify
```bash
uv run --no-sync atobar-ops verify
```
