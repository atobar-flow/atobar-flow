# J41 (harness-guardrails-from-seq05): Harness Guardrails from the SEQ-05 Retro — One CI-Parity `verify`, `ship`/`review`/`jumbo` Fixes, and Mechanical Guards Instead of Prose Rules

- **GitHub Issue:** [#333](https://github.com/atobar-flow/atobar-flow/issues/333)
- **Priority:** P1 (Contributor-harness efficiency & review-finding prevention)
- **Status:** Completed (PR #334)
- **Milestone:** M07 (backlog)
- **Blocked by:** none
- **Blocks:** none (complements J39 (#317) — the PR Reviewer role is where non-mechanical rules from WP-9 eventually get enforced by an agent)
- **Owner:** factory
- **Source:** Pi (Gemini Argon) retro on SEQ-05 (`#204` / PR `#332`, merged `9fdfa81`), reviewed adversarially against `origin/main@9fdfa81`.

---

## Read First
```bash
rg -n "Fast Verification|Codebase Map|House Rules" AGENTS.md
rg -n "def cmd_ship|def cmd_jumbo_new|p_ship" atobar-ops/src/atobar_ops/cli.py
rg -n "pytest|schema --check|manifest|gitleaks" .github/workflows/ci.yml
rg -n "Verify|Grooming" .github/ISSUE_TEMPLATE/roadmap-task.md docs/ROADMAP.md
```

## Goal
Turn the SEQ-05 review findings into **mechanical** guards (tests, CLI gates, fixed fakes) that every harness — Pi, Claude Code, Devin, Cursor — hits the same way. Add prose rules only where a check can't be automated, put them where all harnesses already look (PR/review templates and an `rg`-able doc), and keep the always-loaded root `AGENTS.md` **no longer than it is now**.

## Placement principles (harness-neutral)

| Layer | Who sees it | Use it for | Don't use it for |
| :--- | :--- | :--- | :--- |
| `atobar-ops` CLI | Every harness (it's a shell command) | Gates and ops: verify, ship, review, jumbo | Explanations |
| `tests/test_boundaries.py`, `tests/test_table_ownership.py` | CI, so every PR from every harness | Rules a parser/AST can decide | Judgement calls |
| Test fakes (`MemoryStore`) | Every unit test | Making fakes behave like Postgres | — |
| `.github/PULL_REQUEST_TEMPLATE.md`, `REVIEW_TEMPLATE.md` | Every harness, already mandatory | Judgement rules (concurrency, idempotency, persistence) | Long checklists |
| `docs/*.md` (queried by `rg -n`) | On demand | Detailed checklists with examples | Anything needed every session |
| Root `AGENTS.md` (= `CLAUDE.md` symlink, read by `AI.md`/`.cursorrules`) | Every session, always loaded | Orientation, pointers, house rules | Domain checklists |
| `.pi/skills/*` (+ `.claude/skills` symlink) | Pi, Claude Code | Operational workflows | Coding rules |

Single source of truth plus symlink shims: canonical files stay where they are, and each harness-specific entry point is a symlink, never a copy.

---

## Part 1 — Adversarial review of the Pi retro

| ID | Pi claim | Verdict | Evidence (`origin/main@9fdfa81`) | Correction |
| :--- | :--- | :--- | :--- | :--- |
| **A1** | "4-place wiring tax" causes drift in CP's local fallback transport | **Confirmed — but the fix only treats the symptom** | `control-panel/src/control_panel/flow_engine_client.py:687` `_build_local_fallback_transport` re-implements FE routes by hand | A checklist only lowers the odds of drift. The root fix is to stop hand-duplicating FE routes (follow-up issue, see Out of scope). Short term: checklist in a doc (WP-9), not in root `AGENTS.md`. |
| **A2** | Ban `hasattr(store, …)` in `contracts/` + `flow-engine/` via an AST test | **Confirmed, under-scoped, and would fail on day one** | ~10 existing hits in `contracts/src/atobar_contracts/conductor_io.py:230-504` and ~10 in `control-panel/src/control_panel/app.py:278-523` | Make it a **ratchet**: a checked-in per-file baseline count that can only go down, covering all three `src/` trees (WP-7). |
| **B1** | `MemoryStore` returns live dicts, hiding non-persisted mutations (P0 #3) | **Confirmed** | `flow-engine/src/flow_engine/store.py:1512` returns `self.run_creds.get(...)`; the Supabase twin at `:760` returns `dict(row)` | Agents can't follow a "never mutate the returned dict" rule reliably. **Fix the fake** so getters return deep copies, and add a behavioural test (WP-6). |
| **B2** | UserData → `sudo --preserve-env` bridge was untested (P0 #2) | **Confirmed, fixed in #332, guard scoped too narrowly** | `cloud_init.sh.tpl:165,168` now include `FLOW_GOAL_*`; but `launch_spot_instance` also exports non-`FLOW_*` names (`ISSUE_TEXT`, `TARGET_REPO`, `CONTROL_PANEL_URL`, `ENGINE_REF`, `LABEL_NAME`, …) | Guard must assert **every exported name** ⊆ preserve-env on **both** sudo lines, obtained behaviourally from a fake EC2 call (pattern: `tests/test_metric_compiler_e2e.py:43,70`), not by regexing Python (WP-7). |
| **C** | Concurrency rules: atomic counter RPCs, `claim_*` with TTL, `_lock_for_issue` + `has_processed_event` | **Valid engineering, not mechanically checkable** | P1 #5–#7 on PR #332 | Not for root `AGENTS.md`. Becomes 3 rows in the PR self-review and review templates, linking `docs/CROSS_SERVICE_CHANGES.md` (WP-9). Long term, J39's PR Reviewer enforces it. |
| **D1** | Pytest basename collision → require globally unique `test_*.py` names | **Root cause misdiagnosed** | Duplicate basenames **already exist and pass**: `control-panel/tests/test_key_delivery.py` and `flow-agent/tests/runtime/test_key_delivery.py` (the latter dir has `__init__.py`). The failure needs two non-package dirs under default `prepend` import mode. The blind spot is that **`.github/ISSUE_TEMPLATE/roadmap-task.md` Verify runs pytest per package**, while CI runs a single root `pytest -q` (`ci.yml:42`) | Drop the naming rule. Set `--import-mode=importlib` (WP-5) and make every instruction surface call one CI-parity `atobar-ops verify` (WP-1, WP-9). |
| **D2** | Gitleaks false positive on `key="p95_latency_ms"` → avoid 14+ char strings with digits | **Gap confirmed; mitigation rejected** | `ci.yml:48-58` runs gitleaks; `ship` doesn't; only `.gitleaksignore` fingerprints exist | Renaming domain metric keys to dodge a scanner damages the product vocabulary (tenants use `p95_latency_ms`). Instead: a local scan in `verify`, plus a **narrow** `.gitleaks.toml` allowlist regex for snake_case metric identifiers (WP-8). |
| **D3** | `ship` lacks `--body-file` and can't update an existing PR | **Confirmed, and incomplete — Pi missed 3 bugs** | `cli.py:754` refusal gate ignores `mypy_res.returncode`; `ship` never runs `schema --check` / `manifest` (CI does, `ci.yml:44-46`); SKILL.md says "Targeted tests" but `cli.py:733` runs the full suite | WP-1 (gates) + WP-2 (ergonomics). |
| **D4** | — (not raised) `jumbo new` is unsafe | **New finding (reproduced while filing this issue: local files stop at J39, but J40 was already #326)** | `cmd_jumbo_new` overwrites `issues/J<NN>-*.md` without an existence check and posts the empty scaffold as the issue body. `jumbo new 39 …` today would clobber the untracked J39 draft | WP-3. |
| **E** | — (not raised) The always-loaded instructions are stale | **New finding — costs every harness turns on every session** | Root `AGENTS.md`: map lists `flow-engine/conductor/` (actual `flow-engine/src/flow_engine/conductor/`), `tests/control_panel/` (actual `control-panel/tests/`); "Fast Verification" says `pythonpath = ["src", "tests/control_panel"]` (wrong) and `mypy src/` (no root `src/`). `docs/ROADMAP.md` step 5 says `ruff check src/ tests/ && mypy src/`. `.cursorrules` is a hand-copied, already-diverged duplicate of `AI.md` | WP-9. |
| **F** | Add `atobar-ops review <pr>` | **Confirmed** | Reading PR #332's 1 review + 13 inline comments needed raw `gh api` (no `atobar-ops` coverage) | WP-4, including resolved/unresolved thread state. |
| **G** | House rule conflict (not raised) | **New finding** | Root `AGENTS.md` and `docs/AGENTS.md:62` ban tests that "regex-grep source/YAML/SQL/Terraform", yet Pi's guards (and existing `tests/test_supabase_boundary.py`, `test_no_gha_business_logic.py`) parse source | Add a narrow written exception: **structured parity/ratchet guards in `tests/test_boundaries.py` and `tests/test_table_ownership.py` only** (WP-9). |

---

## Work Packages (one PR each, `Refs #<this>`; ≤ ~3 source files + tests)

- [x] **WP-1: `atobar-ops verify` — one CI-parity gate.**
  New `atobar-ops/src/atobar_ops/verify.py` + `verify` subcommand. It runs exactly the `ci.yml` steps: `ruff check .`, per-package `mypy`, root `pytest -q`, `python -m atobar_contracts.schema --check`, `python -m atobar_contracts.manifest`, and `gitleaks git` over `origin/main..HEAD` if `gitleaks` is on `PATH` (otherwise it prints `SKIPPED: gitleaks not installed`, which is not a pass). `--changed` runs targeted pytest/ruff on paths changed vs `origin/main` for fast iteration; it is never used as the ship gate. `cmd_ship` calls `verify` (full mode), and **any** non-zero step refuses the ship (fixes `cli.py:754` ignoring mypy).
  Tests: `atobar-ops/tests/test_verify.py` (fake `timebox.run`; a parity test that loads `ci.yml` with a YAML parser and asserts every `run:` command in the `test` job maps to a verify step).
- [x] **WP-2: `ship` ergonomics.**
  `--body-file PATH` (mutually exclusive with `--body`). On an existing open PR with `--title`/`--body[-file]` passed → `gh pr edit` instead of the silent "PR already exists". Without `--merge`, wait for checks on the pushed `head_sha` within `--timeout` (default ≤ 240 s) and print one summary; on failure, reuse the `ci` subcommand's failed-step log tail. Fix SKILL.md's `ship` row (it says "targeted tests").
- [x] **WP-3: `jumbo` safety.**
  `jumbo new` refuses to overwrite an existing `issues/J<NN>-*.md`, and refuses if `J<NN>` already appears in `JUMBO_STATUS.md` or in an open `jumbo`-labelled issue title. New `--from-file PATH` posts a pre-written body. New `jumbo sync <NN>` pushes the local file to the issue body (`gh issue edit --body-file`). Add both to SKILL.md.
- [x] **WP-4: `atobar-ops review <pr> [--repo] [--unresolved]`.**
  Read-only and bounded. Prints the latest review state per reviewer, top-level review bodies, and inline comments as `path:line — body`, grouped by thread with resolved/outdated flags (GraphQL `reviewThreads`). Output is truncated with the standard `--max-chars`. SKILL.md row: "Use instead of `gh api …/pulls/N/comments`".
- [x] **WP-5: Pytest import mode.**
  Add `addopts = "--import-mode=importlib"` to root `pyproject.toml` `[tool.pytest.ini_options]`, and run the full suite to prove nothing relies on `sys.path` side-effects from test dirs. Helpers under `control-panel/tests` stay importable via `pythonpath`. If something does break, fall back to adding `__init__.py` to every `tests/` subtree, and record which option was used.
- [x] **WP-6: `MemoryStore` copy-on-read.**
  `flow-engine/src/flow_engine/store.py` and `control-panel/src/control_panel/store.py`: every `MemoryStore` getter that returns `dict`/`list`/model state returns a `copy.deepcopy` (models via `model_copy(deep=True)`). Add a parametrised behavioural test that mutates every getter's return value and asserts a re-read is unchanged, so P0 #3-class bugs fail in unit tests the way they fail on Postgres.
- [x] **WP-7: Parity & ratchet guards in `tests/test_boundaries.py`.**
  (a) `test_spot_userdata_exports_survive_sudo`: call `launch_spot_instance` against the fake EC2 from `tests/test_metric_compiler_e2e.py`, collect the exported names by executing UserData with `bash -c '…; env -0'`, and assert they are ⊆ the `--preserve-env=` list on **every** `sudo` line of `cloud_init.sh.tpl` (parsing a structured shell token). Exemption list for vars intentionally not forwarded, each with a comment.
  (b) `test_store_hasattr_ratchet`: AST-count `hasattr(<name containing "store">, …)` per file across `contracts/src`, `flow-engine/src`, `control-panel/src`, compared against `tests/hasattr_store_baseline.json`. Fail if any file goes up; print the "lower the baseline" hint when one goes down.
- [x] **WP-8: Gitleaks parity, without renaming metrics.**
  Add `.gitleaks.toml` with `[extend] useDefault = true` and an allowlist `regexes` entry for snake_case metric identifiers with a unit suffix (e.g. `^[a-z][a-z0-9_]*_(ms|s|pct|usd|count|ratio|bytes)$`), scoped to the `generic-api-key` rule. Include a test fixture proving a realistic fake token in the same file is **still** flagged. Point the CI action at the config.
- [x] **WP-9: Instruction layer — fix the stale parts, add pointers, net-zero growth for root `AGENTS.md`.**
  1. Root `AGENTS.md`: fix the Codebase Map paths (E) and replace "Fast Verification" with `uv run --no-sync atobar-ops verify [--changed]`. Add **one** line: "Cross-service change (contracts ↔ flow-engine ↔ control-panel ↔ spot UserData)? `rg -n` `docs/CROSS_SERVICE_CHANGES.md`." Add the narrow structured-guard exception (G). Line count must be ≤ today's 92.
  2. New `docs/CROSS_SERVICE_CHANGES.md` (`rg`-friendly, ≤ 120 lines): the 4-place wiring list, the spot env handover, the store-protocol-not-`hasattr` rule, and the concurrency rules (atomic RPC counters, `claim_*` with TTL, every turn-complete branch inside `_lock_for_issue` + `has_processed_event`). Each rule that is enforced mechanically names its guard test or CLI gate.
  3. `.github/PULL_REQUEST_TEMPLATE.md` corner-case audit: 3 prefilled rows (concurrency/idempotency, persistence round-trip on the real store, sandbox handover). `.github/REVIEW_TEMPLATE.md`: matching checks.
  4. `.github/ISSUE_TEMPLATE/roadmap-task.md` Verify block and `docs/ROADMAP.md` grooming step 5 → `atobar-ops verify` (removes the per-package pytest loop behind D1).
  5. Shims: replace `.cursorrules` with a symlink to `AI.md`. Confirm how Devin discovers repo instructions and skills (root `AGENTS.md`? a skills path?) against current Devin docs. Add a symlink shim only if a documented path exists; never a copy. Add `tests/test_harness_shims.py` asserting each shim (`CLAUDE.md`, `.cursorrules`, `.claude/skills/atobar-ops`, plus any Devin shim) is a symlink resolving to its canonical file.

## Out of scope
- **Removing the 4-place wiring tax at its root**, i.e. replacing `_build_local_fallback_transport` with the real FE app over a `MemoryStore` via `httpx.ASGITransport`. `tests/test_boundaries.py` forbids `control_panel` → `flow_engine` imports, so this needs a root-level test harness design. Separate follow-up issue.
- Driving the `hasattr(store, …)` baseline down to zero (the ratchet only stops it growing).
- J39's PR Reviewer role. WP-9's template rows are the interim, human/agent-filled version of that check.
- Any new skill: the checklist is a coding rule (doc + templates), not an operational workflow.

## Corner Cases & Simplification Audit
- **Corner cases per component:**
  - `verify` (WP-1): `gitleaks` absent locally → explicit `SKIPPED` line, never a silent pass. A slow full suite near the 240 s budget → per-step timeouts and an overall deadline, with the failing step named. Dirty working tree → verify what's on disk, but `ship` refuses uncommitted changes.
  - `ship --body-file` (WP-2): file missing or not UTF-8 → refuse before pushing. Existing PR is `CLOSED`/`MERGED` → create a new one (keep the current check). Checks never registering → bounded wait, then `EXIT_TIMEOUT`, not a hang.
  - `jumbo` (WP-3): race between two agents choosing the same `J<NN>` → check the GitHub issue titles, not just local files.
  - `review` (WP-4): >100 threads → paginate with a cap and print `(truncated)`. Reviews from bots and humans → show the author login and don't filter.
  - `MemoryStore` copies (WP-6): tests that currently rely on aliasing (mutate the returned row, expect the store to change) will fail. Fix the test through the store API, not by exempting the getter.
  - Spot-env guard (WP-7a): vars set inside `cloud_init.sh.tpl` itself (e.g. `INSTANCE_ID`, `DISPLAY`) are in preserve-env but not exported by UserData — the guard is one-directional (exports ⊆ preserve).
  - Gitleaks allowlist (WP-8): the regex must not match high-entropy tokens that happen to end in `_ms` — the fixture test proves it.
- **Assumptions challenged / simplifications:**
  - "Agents need more rules in `AGENTS.md`" → rejected. Each always-loaded line costs every harness every session. Rules that can be checked become tests, and judgement rules go into templates the agents already fill in.
  - "Unique test basenames" → rejected (D1). A config line removes the whole class.
  - "Rename metrics to appease gitleaks" → rejected (D2).
  - Duplicated harness entry files (`.cursorrules` ≈ `AI.md`) → collapse to symlinks.

## Acceptance criteria (tests first)
- [x] AC-1: `atobar-ops verify` runs the same steps as `ci.yml` (parity test green), and `ship` refuses on any failing step, mypy included.
- [x] AC-2: `ship --body-file` works, and re-running `ship` on an open PR updates its title/body. Without `--merge` it returns one bounded checks summary.
- [x] AC-3: `jumbo new` cannot overwrite an existing J-file or reuse a J-number; `--from-file` and `jumbo sync` work.
- [x] AC-4: `atobar-ops review 332` prints PR #332's review and its 13 inline comments in ≤ default `--max-chars`, with thread resolution state.
- [x] AC-5: With `--import-mode=importlib`, two same-named test files in non-package dirs collect and pass under root `pytest -q` (temporary fixture test).
- [x] AC-6: Mutating any `MemoryStore` getter's return value doesn't change stored state (flow-engine + control-panel).
- [x] AC-7: Removing any name from either `--preserve-env` line makes `test_spot_userdata_exports_survive_sudo` fail. Adding a `hasattr(store, …)` anywhere in the three `src/` trees makes the ratchet fail.
- [x] AC-8: `gitleaks` with the repo config passes on `key="p95_latency_ms"` and still flags a realistic fake token fixture.
- [x] AC-9: Root `AGENTS.md` ≤ 92 lines with no stale paths/commands; `.cursorrules` is a symlink; `tests/test_harness_shims.py` green. Spot-check: one Pi session and one Claude Code session asked to "add a new FE-owned RPC used by CP" both `rg` into `docs/CROSS_SERVICE_CHANGES.md` and run `atobar-ops verify` without extra prompting.
- [x] Workspace verification green (below).

## Verify
```bash
uv run --no-sync atobar-ops verify          # after WP-1; until then:
uv run --no-sync ruff check .
for m in contracts control-panel flow-engine flow-agent atobar-ops; do uv run --no-sync mypy "$m/src" --ignore-missing-imports || exit 1; done
uv run --no-sync pytest -q                  # root run, same as CI — not per package
uv run --no-sync python -m atobar_contracts.schema --check
uv run --no-sync python -m atobar_contracts.manifest
```

## Risks
- `--import-mode=importlib` can break tests that import sibling test modules by bare name. WP-5 runs the full suite first and falls back to `__init__.py` packages if needed.
- The `hasattr` ratchet baseline can be "fixed" by bumping the JSON. Review must treat baseline increases as a red flag, and the PR template row from WP-9 asks for it explicitly.
- Devin's instruction/skill discovery is unverified here. WP-9.5 must cite the doc it relied on, not assume.
