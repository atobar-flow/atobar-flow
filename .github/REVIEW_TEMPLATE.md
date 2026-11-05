## Adversarial PR Review Checklist (`atobar-flow`)

See `docs/CROSS_SERVICE_CHANGES.md` for the cross-service wiring, concurrency, persistence, and sandbox handover rules.

### 1. Per-Detail Corner-Case Verification
- [ ] **Concurrency, Atomic Counters & Lock Scope (`docs/CROSS_SERVICE_CHANGES.md` §4):** Do cross-PR counters use atomic Postgres RPCs (`SET col = col + $2`), do webhook/metric Spot dispatches use a `claim_*` TTL guard, and do all `turn-complete` branches run inside `_lock_for_issue` + `has_processed_event`?
- [ ] **Persistence Round-Trip (`docs/CROSS_SERVICE_CHANGES.md` §1, §3):** Are all state mutations persisted through the `Store` protocol (never mutating a dict returned by a getter), and are new tables/RPCs registered in `tests/test_table_ownership.py`?
- [ ] **Spot Sandbox Handover (`docs/CROSS_SERVICE_CHANGES.md` §2):** Are all new `launch_spot_instance` `UserData` exports forwarded in both `sudo --preserve-env=` lines of `cloud_init.sh.tpl` and persisted via `create_run_credential` kwargs?
- [ ] **Partial Failure & Retry Idempotency:** If a sweep, webhook, or DB write fails mid-way and is retried, does it avoid duplicate side effects (e.g., duplicate `loop_rounds`, duplicate terminal `sessions`, or un-terminated resources)?
- [ ] **Pagination, Bounds & Empty/Null Inputs:** Are multi-page AWS/GitHub responses, empty collections, and clock/expiry boundaries (`expires_at - 60s`, `0`, `None`) handled deterministically?
- [ ] **Auth, Provenance & Tenant Isolation:** Are internal tokens, run credentials, and tenant boundaries enforced fail-closed?

### 2. Assumption & Simplification Audit
- [ ] **Zero Duck-Typing / Ratchet Integrity:** Does the PR avoid adding `hasattr(store, ...)` fallbacks (any increase in `tests/hasattr_store_baseline.json` is a blocker), legacy tag chains, or fake state fabrication in production adapters?
- [ ] **Single Source of Truth:** Are shared protocols (`Store`), tag contracts (`SpotInstanceTags`), and reaper/engine workflows unified across packages?
- [ ] **Test Hygiene:** Are all acceptance criteria covered by deterministic Python unit tests (no wall-clock `sleep`, no live network calls, no static file regex-grepping outside `tests/test_boundaries.py`, `tests/test_table_ownership.py` and `tests/test_harness_shims.py`)?
