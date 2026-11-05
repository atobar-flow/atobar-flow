# J45 (ci-finops-test-suite): CI FinOps & Test-Suite Modernization — One Billable Job per Push, Margin Under the 60-Second Rounding Cliff, Runaway-Job Caps & a Billed-Minutes Ratchet

> **⛔ Blocked by:** nothing. **✅ Unblocks:** nothing. Independent of J42/J43/J44; it may land at any time.
> **Order inside this issue:** WP-2 (buy margin) must land **with or before** WP-1 (adds ~5s to the test job). WP-3, WP-4, WP-5 are independent. WP-6 runs only if the WP-5 ratchet trips.

- **Priority:** P2 (FinOps headroom, no outage today). It goes to P1 if WP-5 shows the test job's p90 above 50s, or monthly use above 1,500 minutes.
- **Milestone:** M07 (Backlog after the pilot), the same as the J41 harness guardrails.
- **UX Impact:** **None for tenants.** Contributors see one PR check (`test`) instead of two (`test` + `Secret Scan (Gitleaks)`). The leak report moves into the `test` log. SARIF artifacts stop being produced.
- **Review status:** Evidence-based assessment of two external suggestions, done 2026-10-23 against 129 `Python CI` runs (2026-10-11 → 2026-10-23), the job/step timings from the Actions API, and a local profile of the suite. See "Review log" at the bottom.

---

## FINAL VERDICT AND TODO:

**Verdict:** Suggestion **#1 (fold gitleaks into `test`) is correct and is the biggest lever**: the separate job runs 6–13s but is billed a full minute every time, i.e. **~330 of ~990 billed minutes/month (≈ 33%)**. Suggestion **#2 is right about the cliff but wrong about the cause and the current state**. (a) The `Install actionlint` step takes **0–1s** (median 0s), so caching it saves nothing. (b) The "1 in 5 runs over 60s" figure comes from a window that mixes two eras. **Before 2026-10-19** every `test` job took 62–85s (pytest 45–63s), so every one was billed 2 min. The J33 monorepo refactor (#315) cut pytest to ~15s. **Since then, 0 of ~90 runs went over 60s** (31–50s, p90 46s). **The cliff is still a live risk:** pytest's median grew from ~15s to ~20s in four days (2026-10-19 → 10-23), and WP-1 adds ~4–6s. So margin has to be bought **before** folding gitleaks in. The cheapest margin is **mypy**: CI runs it five times, re-checking `contracts` each time. One combined invocation is 3s vs 7s cold locally (≈ 11–14s → ~5s in CI). The largest *unbounded* risk found is not a rounding cliff at all: **no job in any workflow sets `timeout-minutes`**, so a single hung PTY/subprocess test can burn the 360-minute default, **≈ 18% of the 2,000-minute Free quota in one run.**

**TODO (in order):**
1. [x] **WP-2 — Buy margin: one mypy invocation (lands first or with WP-1).** −5…−9s per run, no new dependency.
2. [x] **WP-1 — Fold gitleaks into the `test` job as a pinned, checksummed binary.** −1 billed minute per run (~−330 min/month); no SARIF artifacts; least-privilege `permissions`; a correct scan range on `push`.
3. [x] **WP-3 — Runaway caps & correctness:** `timeout-minutes` on every job in every workflow, `cancel-in-progress` only for PRs, `faulthandler_timeout` for hang diagnostics, and `--durations=10` in CI output.
4. [x] **WP-4 — actionlint supply-chain pin:** verify the downloaded tarball's sha256 (not caching, since that saves 0s).
5. [x] **WP-5 — Billed-minutes ratchet:** `atobar-ops ci --minutes` (read-only) reports billed minutes per job (rounded up per job), p50/p90/max seconds, and runs inside the 55–60s danger band, plus structural CI-budget guards in `tests/test_boundaries.py`.
6. [x] **WP-6 — Contingency (only if WP-5 shows `test` p90 > 50s for 20 consecutive runs):** a `mypy` cache restored from `main`, then `pytest-xdist -n 2` behind a flake audit. (Not triggered: `test` p90 is 46s before WP-2 and ~38–42s after WP-2).
7. [x] **Rejected (with evidence, see Part 2):** caching actionlint; a docs-only `paths-ignore`; skipping CI on `push` to `main`; merging the Control Panel build into `test`; per-test `pytest-timeout` dependency.

---

## Part 1 — Measured Baseline ("As-Implemented", 2026-10-23)

### 1.1 Billing model this issue optimises against

- The repo is **private** and the owner account is on **GitHub Free**. Inferred from `GET /branches/main/protection` → `403 "Upgrade to GitHub Pro or make this repository public"`. This plan includes **2,000 Actions minutes/month** for private repos.
- GitHub bills **each job** rounded **up** to the whole minute. A 7s job and a 59s job both cost 1 minute; 61s costs 2.
- With the default **$0 spending limit**, private-repo Actions **stop** when the quota runs out. House rule: *"external CI on `head_sha` is the only proof of done"*. So quota exhaustion **halts the factory**, not just the budget. That's the FinOps risk this issue manages.

### 1.2 Volume and billed minutes (window 2026-10-11 04:32 → 2026-10-23 07:55, ≈ 12.1 days; ×2.48 to get monthly)

| Workflow / job | Runs in window (PR / push) | Wall time per job | Billed min/run | ≈ Billed min/month |
| :--- | :---: | :--- | :---: | ---: |
| `Python CI` / `test` | 134 (89 / 45) | **since 10-19:** 31–50s (median 42s, p90 46s); **before 10-19:** 62–85s | 1 (was 2) | ~332 |
| `Python CI` / `Secret Scan (Gitleaks)` | 134 | 6–13s (median 9s), scan step 3s | **1** | **~332** |
| `Control Panel Build and Security Scan` | 102 (71 / 31) | 29–33s | 1 | ~253 |
| `Supabase Migrations` | 10 (push / dispatch) | 30–46s | 1 | ~25 |
| `Packer AMI Build & Publish` | 5 | validate 9–12s; build 218–289s | 1 + 4–5 | ~40–50 |
| **Total (current)** | | | | **≈ 990 / 2,000** |
| Legacy scheduled workflows (Conductor Event Processor, Event Bus Dispatcher, Instance GC, Supabase Keep-Alive) | 148 | — | — | **already 0**: last runs 10-16…10-19, files deleted (J43 WP-1) |

Projected after WP-1: **≈ 660 min/month (−33%)**. For context, the pre-10-19 rate (3 billed min per CI run plus the legacy cron workflows) was ≈ 1,700 min/month, close to the cap.

### 1.3 Where the `test` job's seconds go (since 2026-10-19; 31 recent runs)

| Step | Median | Max | Note |
| :--- | ---: | ---: | :--- |
| `Test (pytest)` | 19s | 22s | 836 tests; **10.0s locally**, slowest single test 0.55s; no test > 1s. CI is a 2-vCPU runner. Trend: ~15s (10-19) → ~20s (10-23). |
| `Type check (mypy)` | 11s | 14s | 5 sequential invocations; `contracts` is re-analysed by every package that imports it. |
| `Install dependencies` (`uv sync --locked --all-packages`) | 4s | 7s | uv cache already enabled. |
| `setup-uv`, `checkout`, `Set up job` | 1–2s each | 3–4s | fixed overhead. |
| `Install actionlint` | **0s** | 1s | **Suggestion #2's target saves nothing.** |
| `ruff`, schemas, manifests | 0s | 1s | |

### 1.4 Local mypy experiment (no code changed)

| Mode | Wall time (local, 16 cores) | Result |
| :--- | ---: | :--- |
| 5 × `mypy <pkg>/src --ignore-missing-imports` (cold, `--cache-dir=/dev/null`) — **today's CI** | 7s | 0 issues in 17+12+19+71+10 files |
| 1 × `mypy contracts/src control-panel/src flow-engine/src flow-agent/src atobar-ops/src --ignore-missing-imports` (cold) | **3s** | 0 issues in **129** files (same set, no duplicate-module errors) |
| same, warm cache (37 MB) | ~0s | — |

The only mypy config is root `pyproject.toml [tool.mypy]`; no package has its own section. So a combined run uses **exactly the same strictness**.

### 1.5 Gitleaks behaviour today (`gitleaks/gitleaks-action@v2`, gitleaks 8.24.3)

- `push`: runs `gitleaks detect … --log-opts=-1`, so it scans **only the last commit** of the push. A multi-commit direct push to `main` (e.g. `36597dd chore(ci)` landed without a PR) is only partly scanned.
- `pull_request`: scans the PR's commits via the API (needs `pull-requests: read`, which nothing else in `ci.yml` uses).
- Uploads a `gitleaks-results.sarif` artifact on every run: **374 artifacts** live (678 KB total, so storage cost is negligible). The clutter is real; the dollars aren't.
- Local drift: the contributor laptop has gitleaks **8.24.2**, CI uses 8.24.3. `atobar-ops verify` scans `origin/main..HEAD` + `--pre-commit`, so it has no notion of a `push` range.
- Nothing in the codebase or docs depends on the check name `Secret Scan (Gitleaks)` (`rg` clean). Branch protection and required checks aren't available on this plan, so removing the check can't wedge a PR.

---

## Part 2 — Assessment of Every Lever (accepted / rejected)

| # | Lever | Evidence | Saving | Decision |
| :--- | :--- | :--- | :--- | :--- |
| S1 | **Fold gitleaks into `test`** (suggestion #1) | 134/134 gitleaks jobs billed 1 min for 6–13s of work | **~330 min/month (−33% total)** | **Accept → WP-1** |
| S2a | **Cache the actionlint binary** (suggestion #2) | step median 0s, max 1s | ~0 | **Reject.** Replace with a checksum pin (WP-4), which is a supply-chain gain, not a time gain. |
| S2b | **Keep `test` under 60s** (suggestion #2's goal) | 0/~90 runs > 60s since 10-19, but pytest +5s in 4 days and WP-1 adds ~5s | avoids 2× billing on ~332 runs/month (up to −332 min/month in the bad case) | **Accept the goal → WP-2 (margin) + WP-5 (ratchet)** |
| L1 | Single mypy invocation | §1.4: 7s → 3s locally, identical file set and config | −5…−9s/run of margin | **Accept → WP-2** |
| L2 | `timeout-minutes` on every job | none set anywhere; default 360 min | caps the worst case at 10 min instead of 360 (−350 min per hang) | **Accept → WP-3** |
| L3 | `cancel-in-progress` only on PRs | today two quick merges to `main` cancel the first `main` run, so that merge commit is never verified on `head_sha` | ~0 min; correctness | **Accept → WP-3** |
| L4 | Billed-minutes report + CI structure guards | no visibility today; this assessment needed ad-hoc `gh api` calls | prevents regressions | **Accept → WP-5** |
| L5 | `mypy` cache via `actions/cache` | warm ≈ 0s, but 37 MB per entry; per-SHA keys churn the 10 GB repo cache (608 MB / 32 entries today) and could evict the uv cache | −3…−5s | **Contingency → WP-6** (restore on PR, save only on `main`) |
| L6 | `pytest-xdist -n 2` | 836 tests in 10s serial; 2-vCPU runner; PTY/subprocess/env-mutating tests are flake candidates | −5…−8s | **Contingency → WP-6**, behind a 3×-repeat flake audit |
| L7 | `paths-ignore` for docs-only changes | **0 of 36** non-merge `main` commits and **0 of 40** merged PRs were docs-only; `pull_request` path filters use the whole PR diff | ~0 | **Reject** |
| L8 | Skip CI on `push` to `main` (45 runs ≈ 110 min/month after WP-1) | Free plan can't enforce "branch up to date", so the `main` tree can differ from the PR head; violates "CI on `head_sha` is proof of done" | ~110 min/month | **Reject** (correctness > minutes) |
| L9 | Merge the CP build (30s) into `test` | 42s + 30s = 72s → 2 billed min, same as two 1-min jobs | 0 | **Reject** |
| L10 | `pytest-timeout` plugin | new dependency; job-level `timeout-minutes` + built-in `faulthandler_timeout` cover kill and diagnosis | 0 | **Reject** (use built-ins, WP-3) |
| L11 | Bump `actions/checkout@v4` / `setup-uv@v5` and pin action SHAs | not a minutes lever | 0 | **Out of scope** (separate supply-chain backlog item) |
| L12 | Delete the 374 stale SARIF artifacts | 678 KB total | ≈ $0 | **Not needed.** They expire with retention after WP-1 stops new uploads. |

---

## Part 3 — Work Packages

- [x] **WP-2 (L1 — buy margin first): one mypy invocation**
  - `.github/workflows/ci.yml` `Type check (mypy)`: replace the `for m in …` loop with **one** line:
    `uv run --no-sync mypy contracts/src control-panel/src flow-engine/src flow-agent/src atobar-ops/src --ignore-missing-imports`.
  - `atobar-ops/src/atobar_ops/verify.py` `ci_test_commands()`: replace the per-package `mypy:{pkg}` steps with a single `("mypy", [... *[f"{p}/src" for p in WORKSPACE_PACKAGES], "--ignore-missing-imports"], 90)` so `verify` stays CI-parity. Keep `_changed_verify_steps` per-package (it's for fast local iteration, and a subset of packages is the point there).
  - `atobar-ops/tests/test_verify.py::test_ac1_ci_yml_test_job_commands_parity_with_verify`: drop the `for m in` special case. The single line now matches verbatim through the generic branch.
  - **Self-tests:**
    - `test_wp2_ci_test_commands_has_single_mypy_over_all_packages`: exactly one step named `mypy`; its argv contains every `f"{p}/src"` in `WORKSPACE_PACKAGES`, **in that order**, and `--ignore-missing-imports`.
    - The existing parity test passes unchanged in intent: every `run:` line in `jobs.test.steps` ∈ `verify.ci_test_commands()`.
    - `test_wp2_mypy_package_list_matches_workspace_dirs`: `WORKSPACE_PACKAGES` equals the set of top-level dirs with `src/` + `pyproject.toml` (guards a new package being silently left out of type-checking).
    - Manual proof (PR description): local `mypy` combined prints `Success: no issues found in 129 source files` (= 17+12+19+71+10), and the CI step time from WP-5's report before vs after.

- [x] **WP-1 (S1): gitleaks as a step in `test`, delete the `gitleaks` job**
  - `ci.yml`:
    - `actions/checkout@v4` in `test` gets `fetch-depth: 0` (`.git` is 6.4 MB / 420 commits, ~1s).
    - New step `Install gitleaks` (name starts with `Install `, so the parity test skips it as setup): download `gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz` from the official release, **verify sha256** against a value committed in the workflow (`echo "<sha256>  gitleaks.tgz" | sha256sum -c -`), extract to `$HOME/.local/bin`. `GITLEAKS_VERSION: "8.24.3"` (the version CI uses today), as job-level `env`. Take the checksum from the release's `gitleaks_8.24.3_checksums.txt` at implementation time; never hand-type it.
    - New **last** step `Secret scan (gitleaks)` with `if: ${{ !cancelled() }}`, so a leak is reported even when lint/tests fail earlier, and a test failure never hides a leak. Compute the range in the step:
      - `pull_request`: `${{ github.event.pull_request.base.sha }}..${{ github.event.pull_request.head.sha }}`
      - `push`: `${{ github.event.before }}..${{ github.sha }}`; if `before` is all zeros (first push of a ref) or missing from the clone (force-push), fall back to `-1`.
      - Run: `gitleaks git --no-banner --redact --config .gitleaks.toml --log-opts="$RANGE"`. No SARIF, no artifact upload, no `GITHUB_TOKEN`.
    - Delete the `gitleaks:` job. Drop `pull-requests: read` from top-level `permissions` (only the action used it), leaving `contents: read`.
  - `atobar-ops/src/atobar_ops/verify.py`: extract the flags into **one** source of truth, `gitleaks_base_argv(binary: str) -> list[str]` (`[binary, "git", "--no-banner", "--redact", "--config", ".gitleaks.toml"]` when the file exists), and add `GITLEAKS_VERSION = "8.24.3"`. `run_verify` uses it (unchanged behaviour: `origin/main..HEAD` + `--pre-commit`). When the local `gitleaks version` differs from `GITLEAKS_VERSION`, print one `gitleaks: NOTE: local 8.24.2 != CI 8.24.3` line. It's a warning, not a failure.
  - Parity test: handle the `Secret scan (gitleaks)` step by name. Assert its command starts with the same flags as `gitleaks_base_argv("gitleaks")[1:]` and contains `--log-opts`. Assert the workflow `env.GITLEAKS_VERSION == verify.GITLEAKS_VERSION`.
  - **Self-tests:**
    - `test_wp1_ci_has_single_billable_job_on_pull_request` (YAML-structural, `tests/test_boundaries.py`): `ci.yml` has exactly one job, and no step `uses:` anything starting with `gitleaks/gitleaks-action`.
    - `test_wp1_gitleaks_step_runs_when_previous_steps_fail`: the gitleaks step's `if` contains `!cancelled()` and it's the last step.
    - `test_wp1_ci_permissions_least_privilege`: top-level `permissions == {"contents": "read"}`.
    - `test_wp1_gitleaks_version_parity`: workflow env version == `verify.GITLEAKS_VERSION`; the install step contains `sha256sum -c`.
    - `test_wp1_gitleaks_base_argv_shared` (`atobar-ops/tests/test_verify.py`): with a fake `.gitleaks.toml` in `tmp_path` (`monkeypatch.chdir`), `gitleaks_base_argv` includes `--config .gitleaks.toml`; without it, it doesn't. `run_verify` (gitleaks monkeypatched via `timebox.run`) is called with exactly `gitleaks_base_argv(...) + ["--log-opts=origin/main..HEAD"]`.
    - Existing `tests/test_boundaries.py::test_ac8_gitleaks_config_allows_metric_keys_and_flags_fake_tokens` still passes (config unchanged).
    - **Live proof (PR description, not a unit test):** (1) A throwaway commit on the PR branch adding a fake token matching a default rule (e.g. `ghp_` + 36 chars), so the `test` check goes **red at the gitleaks step** with the redacted finding in the log. Then drop the commit with `git reset` + force-push (never merged). (2) The green run shows **one** check and `atobar-ops ci --minutes` (WP-5) shows 1 billed minute.

- [x] **WP-3 (L2, L3, L10): runaway caps & correctness**
  - `timeout-minutes` on **every** job: `ci.yml/test: 10`; `control_panel_build.yml: 10`; `supabase-migrate.yml: 10`; `packer-build.yml` validate `5`, build `30` (today's max 289s, ×6 headroom).
  - `ci.yml` concurrency: `cancel-in-progress: ${{ github.event_name == 'pull_request' }}`. Superseded PR pushes are still cancelled; every `main` commit is verified.
  - `pyproject.toml [tool.pytest.ini_options]`: `faulthandler_timeout = 60`. This is built into pytest, so no dependency: a hung test dumps all thread stacks after 60s, before the job timeout kills the runner, so the log shows *which* test hung.
  - CI pytest command: `uv run --no-sync pytest -q --durations=10` (mirror it in `verify.ci_test_commands()`), so slow-test drift is visible in every log.
  - **Self-tests** (`tests/test_boundaries.py`, structural YAML):
    - `test_wp3_every_workflow_job_has_bounded_timeout`: for every `.github/workflows/*.yml`, every job has an integer `timeout-minutes` in `[1, 30]`. A new workflow without one fails CI.
    - `test_wp3_ci_cancels_only_pull_requests`: the `concurrency.cancel-in-progress` expression references `pull_request`.
    - `test_wp3_faulthandler_timeout_configured`: `tomllib` reads `faulthandler_timeout`, an int in `(0, 120]`.
    - Parity test still green, with `--durations=10` on both sides.

- [x] **WP-4 (S2a replacement): actionlint checksum pin**
  - `Install actionlint`: download to a file, `sha256sum -c` against a committed value for `actionlint_1.7.7_linux_amd64.tar.gz` (from the release's `actionlint_1.7.7_checksums.txt`), then extract. No caching: it already takes 0s.
  - **Self-test:** `test_wp4_ci_binary_downloads_are_checksummed` (`tests/test_boundaries.py`): every step in `ci.yml` whose `run` contains `curl` also contains `sha256sum -c`. This one structural rule covers actionlint, gitleaks and any future download.
  - Unchanged: `control-panel/tests/test_j09_trigger_actionlint.py` and `test_gateway_cutover_j34.py` still find `actionlint` on `PATH` in CI and run. Locally they `skip` when it's absent, as today.

- [x] **WP-5 (L4): billed-minutes ratchet in `atobar-ops`**
  - `atobar-ops ci <owner/repo> --minutes [--workflow W] [--limit N=30]` (read-only, bounded by `--timeout`, truncated output like every subcommand). For the last N completed runs it fetches `actions/runs/{id}/jobs` via the existing `gh` wrapper and prints, per job name: `n`, p50/p90/max seconds, **billed minutes** (`Σ ceil(sec/60)`), the count in the **danger band 55–60s** and **over 60s**, and the **slowest step** median. Then a `NEXT:` hint (`OK` / `WARN: test p90 > 50s, see J45 WP-6` / `CLIFF: N runs billed 2 min`). Pure computation lives in `atobar_ops/ci_minutes.py::summarize(jobs_payloads) -> dict`, with no I/O.
  - `--json` returns the dict for scripts.
  - Update `.claude/skills/atobar-ops/SKILL.md` command table (and the `.pi`/`.agents` shims via the existing symlinks) with one row.
  - **Self-tests** (`atobar-ops/tests/test_ci_minutes.py`, fixtures as JSON dicts, **no network**, with `gh.json_or_empty` monkeypatched):
    - `test_wp5_billing_rounds_up_per_job`: jobs of 7s, 59s, 60s, 61s, 0s (skipped) → billed 1, 1, 1, 2, 0. A job with `started_at`/`completed_at` = `None` is ignored and counted as `incomplete`.
    - `test_wp5_danger_band_and_cliff_counts`: 54s → neither; 55s and 59s → danger; 61s → cliff.
    - `test_wp5_percentiles_small_n`: n=1 and n=2 don't raise; p90 of `[31..50]` is deterministic.
    - `test_wp5_next_hint_thresholds`: p90 = 51s → `WARN`; any >60s → `CLIFF`; otherwise `OK`.
    - `test_wp5_timeout_returns_partial`: a deadline that expires after 3 of 10 runs prints a summary of 3 with a `TRUNCATED` note and exit 0 (read-only command, partial data is useful).
    - `test_wp5_cli_registered`: `build_parser().parse_args(["ci", "o/r", "--minutes"])` sets the flag.

- [x] **WP-6 (L5, L6) — contingency, only if WP-5 shows `test` p90 > 50s across 20 consecutive runs**
  - Step 1: `actions/cache/restore` for `.mypy_cache` on every event; `actions/cache/save` **only** on `push` to `main`. Key `mypy-${{ runner.os }}-${{ hashFiles('uv.lock') }}-${{ github.sha }}`, restore-key prefix `mypy-${{ runner.os }}-${{ hashFiles('uv.lock') }}-`. Pass `--cache-dir .mypy_cache` explicitly. mypy's incremental mode re-checks changed files, so a stale cache is safe.
  - Step 2 (only if Step 1 isn't enough): add `pytest-xdist` as a pinned dev dependency, `-n 2`. **Before** enabling: run the suite 3× with `-n 2 -p no:randomly`, and fix or `xdist_group`-mark every test that touches PTYs, `os.environ`, CWD, fixed ports, or shared files under the repo root.
  - **Self-tests:** `test_wp6_mypy_cache_saved_only_on_main` (structural: the `save` step's `if` references `refs/heads/main`); the xdist flake audit result goes in the PR description (3 green runs, the list of grouped tests).

---

## Part 4 — Edge Cases Audit

| Detail | Concurrency / Race | Partial Failure / Retry Idempotency | Pagination / Bounds / Truncation | Auth / Trust Boundary |
| :--- | :--- | :--- | :--- | :--- |
| **Gitleaks range on `push` (WP-1)** | Two quick pushes: with L3, `main` runs aren't cancelled, so each push scans its own `before..sha`. | `before` = `0000…` (new ref) or unreachable after a force-push → fall back to `-1` and print why. Never pass an invalid range: gitleaks would error out, which reads as "leak found". | `fetch-depth: 0` makes every range resolvable; the repo is 6.4 MB, so revisit only if `.git` > 200 MB. | Strictly better than today's `--log-opts=-1`, which scans only the last commit of a multi-commit push. |
| **Gitleaks range on `pull_request` (WP-1)** | A rebase/force-push of the PR branch → new `head.sha`; `base.sha..head.sha` is recomputed per event. | Re-run of the same job → the same range, the same result (deterministic). | Fork PRs: `head.sha` is in the merge ref fetched by checkout. Range commands run on what's present. | No token needed any more: `GITHUB_TOKEN` is no longer passed to a third-party action. |
| **Gitleaks as the last step with `!cancelled()`** | Concurrency cancel (PR) → skipped, as is correct for a superseded commit. | Earlier step fails → gitleaks still runs, and the job is red for both reasons. A gitleaks install failure (download/checksum) → red with a clear step name, never a silent pass. | Output uses `--redact`; logs never show the secret. | The leak report lives in the job log (private repo); no SARIF artifact is kept for 90 days. |
| **Checksum-pinned binaries (WP-1, WP-4)** | — | A GitHub Releases outage fails the step (same as today). A checksum mismatch fails closed. | — | A tampered release asset is detected; today's `curl \| tar` would run it. |
| **One mypy invocation (WP-2)** | — | A failure lists errors for **all** packages in one output. Today GitHub's `bash -e` stops the loop at the first failing package (verified: no false-green), which hides errors in later packages until the next push. | 129 files; same config. If two packages ever ship the same top-level module name, mypy errors "Duplicate module" — loud, not silent. | — |
| **`timeout-minutes` (WP-3)** | — | A killed job is billed up to the timeout (≤ 10 min), not 360. `faulthandler_timeout=60` prints the hanging test before the kill. | Packer build at 30 min: today's max is 289s. | — |
| **`atobar-ops ci --minutes` (WP-5)** | Runs still `in_progress` are excluded (no `completed_at`). | Read-only; a timeout returns partial data, labelled. | `--limit` ≤ 100; one API call per run, bounded by the deadline; output truncated like every subcommand. | Uses the caller's `gh` auth; reads only `actions/runs/*/jobs`. |
| **Removed `Secret Scan (Gitleaks)` check** | — | Open PRs created before the merge show a stale "expected" check only if it was required. Free plan has no required checks, so nothing waits. | — | `rg` shows no code or doc consumer of the check name. |

---

## Acceptance (whole issue)

1. `uv run --no-sync atobar-ops verify` green, with all new structural tests in `tests/test_boundaries.py` and `atobar-ops/tests/`.
2. On the merge PR: **one** check (`test`), and `atobar-ops ci atobar-flow/atobar-flow --minutes --workflow ci.yml --limit 20` shows `Python CI` = **1 billed minute per run**, `test` max **≤ 55s**, 0 runs in the danger band.
3. The live leak drill from WP-1 is recorded in the PR description (red with a redacted finding → commit dropped → green).
4. 30 days after merge: monthly Actions use ≈ 660 min (±15%), checked with the same report over `--limit 100`.

---

## Review log (2026-10-23 FinOps assessment)

- **Suggestion #1** confirmed and sized from 134 runs: ~330 billed min/month for 3s of scanning. Implementation changed from "use `gitleaks-action` as a step" to a **pinned, checksummed binary**. That drops the token, the SARIF upload and `pull-requests: read`, gives `verify` and CI one flag set, and fixes the `push` range (`-1` → `before..sha`).
- **Suggestion #2** re-scoped: the actionlint step is 0s, so caching it is a no-op. The ">60s in 1 of 5 runs" statistic is pre-J33 (all 62–85s runs are ≤ 2026-10-18). Since 10-19: 0 runs over 60s. The cliff risk is kept and attacked through mypy (the real reducible cost) plus a ratchet.
- **New finding:** no `timeout-minutes` on any job. One hang costs ≈ 18% of the monthly quota. Added WP-3.
- **New finding:** `cancel-in-progress: true` also cancels `main` runs, so a merge commit can go unverified. Fixed in WP-3.
- **Rejected with data:** docs-only path filter (0 docs-only commits/PRs), skipping `main` CI (proof-of-done rule), merging the CP build (72s → still 2 min), `pytest-timeout` (built-ins suffice), xdist/mypy cache (kept as a contingency behind the WP-5 ratchet).
- **Method:** Actions API job/step timings (129 `Python CI` runs; 8 recent runs each of the other workflows), `gh run list` volume, local `pytest --durations=20`, and a cold/warm mypy comparison. No code was changed for this assessment.
