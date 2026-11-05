## Fixes #

## Context & Motivation
<!-- WHY is this change needed? If this is a fix or fast-follow, explain what failure mode or structural debt existed before. -->

## Changes
<!-- Bullet list of what was implemented. Include file paths and key design decisions. -->
-

## Adversarial Self-Review: Corner Cases (per implementation detail) & Assumption Audit
<!-- MANDATORY for all AI agents (pi, Claude Code, Devin, Gemini) and human contributors before opening or stopping on a PR. -->

### 1. Per-Detail Corner-Case Audit
<!-- See `docs/CROSS_SERVICE_CHANGES.md` for concurrency, persistence, and sandbox handover rules. -->
| Implementation Detail (`file:symbol`) | Concurrency / Race | Partial Failure / Retry Idempotency | Pagination / Bounds / Empty Input | Auth / Trust Boundary | Verified By (`test_name`) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Concurrency & Idempotency (`flow-engine` / `conductor_io`) | Atomic counter RPCs (`SET col = col + $2`), `claim_*` TTL before Spot dispatch, `_lock_for_issue` on `turn-complete` | Retried webhooks/turns deduped via `has_processed_event` | <evaluated or N/A> | <evaluated or N/A> | `<test_name or N/A>` |
| Persistence Round-Trip (`SupabaseStore` / `MemoryStore`) | Single-writer table/RPC ownership (`tests/test_table_ownership.py`) | State persisted via store writer API (never mutating getter return dicts; `tests/hasattr_store_baseline.json` not increased) | <evaluated or N/A> | <evaluated or N/A> | `<test_name or N/A>` |
| Spot Sandbox Handover (`launch_spot_instance` → `cloud_init.sh.tpl`) | Per-run flags persisted via `create_run_credential` kwargs | Exported `UserData` vars ⊆ `sudo --preserve-env=` on both `sudo` lines | <evaluated or N/A> | <evaluated or N/A> | `<test_name or N/A>` |
| `<path:symbol>` | <evaluated> | <evaluated> | <evaluated> | <evaluated> | `<test_name>` |

### 2. Assumption & Simplification Audit
- **Assumptions Challenged:** <what over-complicating assumptions in the issue or existing code were rejected?>
- **Dead Shims / Duplication Deleted:** <what `hasattr` duck-typing, legacy tag/schema fallback, or duplicate implementation was removed or unified?>

## Verification
<!-- Paste real command output. Never fabricate or mock terminal output. -->
```bash
uv run --no-sync atobar-ops verify
```

## Architectural Notes
<!-- Optional: deviations from spec, security implications, follow-up work needed. -->
