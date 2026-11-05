# J39: Two-Stage Evaluator Review (Fast Static/Contract Short-Circuit + Progressive-Disclosure Verdict UX)

> **Architecture Decision (Post-Audit Simplification):**
> - **No `pr_reviewer` role:** Since J38 (`#311`), `Developer` → `Evaluator` hands over on the **same warm Spot VM** in `<2s` via `prepare_role_worktree()`, and `Evaluator` already runs with an independent BYOK provider/model (`public.tenant_role_llm`) and `.github/REVIEW_TEMPLATE.md` (`#321`). Static/contract review is folded into **Stage 1 of `Evaluator`** with immediate short-circuit before GUI/Playwright checks.
> - **No `butler` LLM role:** `control-panel` and `flow-engine` remain 100% deterministic (`tests/test_boundaries.py` forbids LLM SDKs). Human steering uses deterministic `flow:*` labels (`#326`), and repo conventions live in version-controlled repo files (`AGENTS.md`, `.github/REVIEW_TEMPLATE.md`) rather than a Control Panel prompt database.

---

## Scope & Work Packages

### WP-1: Structured `file:<path>#L<start>-L<end>` Evidence in `Evaluator` (`flow-agent/src/flow_agent/roles/evaluator/spec.py`)
- Extend `validate_evaluator_finding(finding)` to accept structured repo-relative file/line citations:
  `file:<path>#L<start>` or `file:<path>#L<start>-L<end>` (in addition to `check_run:<id>`, `artifact:(sha256:)?<64-hex>`, and `https?://...`).
- Enforce strict bounds on `file:` evidence:
  - Reject leading `/` or `..` path segments.
  - Require `start >= 1` and `end >= start` (when `end` is present).
  - Continue rejecting free-text claims (e.g., `"it broke on line 42"`).
- Because `atobar_contracts.envelopes.fingerprint()` normalizes `evidence.split(":", 1)[0]` to `"file"`, line-number shifts across commits preserve the finding fingerprint for Conductor's repeat-finding circuit breaker.

### WP-2: Progressive-Disclosure Review Formatting in `post_evaluator_verdict()` (`flow-agent/src/flow_agent/roles/evaluator/spec.py`)
- Add `format_evaluator_review_summary(verdict: Verdict) -> str` and include it in `post_evaluator_verdict()`:
  1. **1-Line Review Header:**
     - `**ATOBAR Evaluator Review** approved \`<sha7>\` (Round N — 0 findings).` when approved with zero findings, or
     - `**ATOBAR Evaluator Review** found K issue(s) on \`<sha7>\` (Round N — \`<verdict>\`).`
  2. **Per-Finding Progressive Disclosure:**
     - Ranked severity badge (`🔴` for `blocker`, `🟡` for `major` / `minor`) + bold `**[id] (criterion)**` + above-the-fold `message`.
     - Collapsible `<details><summary>Learn more</summary>` block with:
       - SHA-pinned GitHub blob permalink (`https://github.com/<tenant>/blob/<head_sha>/<path>#L<start>-L<end>`) when `evidence` is a `file:` citation, or the raw `check_run` / `artifact` / screenshot evidence link.
       - Finding severity and deterministic `fingerprint`.
  3. **Envelope & Mirror Integrity:**
     - Strip any accidental ```` ```flow- ```` fence inside `finding.message` / `finding.criterion` so `atobar_contracts.envelopes.parse()` always round-trips the canonical `render(verdict)` block at the bottom of the comment.

### WP-3: Two-Stage `EVALUATOR_SYSTEM_PROMPT` (`flow-agent/src/flow_agent/roles/evaluator/prompt.py`)
- Structure `EVALUATOR_SYSTEM_PROMPT` into two explicit stages:
  - **Stage 1 — Fast Static & Cross-File Contract Audit (`.github/REVIEW_TEMPLATE.md`):** Inspect the diff and callers/callees at `head_sha` first. If any `blocker` functional or security bug is found, **short-circuit immediately** and emit `flow-verdict` (`"verdict": "changes_requested"`) citing `file:<path>#L<start>-L<end>` without launching slow GUI/X11 checks.
  - **Stage 2 — Test Execution & GUI/TUI Visual Verification (`DISPLAY=:0`):** Run only after Stage 1 passes with zero static/contract blockers.

### WP-4: Roadmap Hygiene (`docs/ROADMAP.md`)
- Remove the obsolete `Epic: #119.` reference and update `M05feat05`–`M05feat08` status rows in `docs/ROADMAP.md` to `Done` (`#204`).

---

## Acceptance Criteria
- [ ] **AC-1 (`file:` Evidence Validation & Path/Line Bounds):** `validate_evaluator_finding` accepts valid `file:<path>#L<start>(-L<end>)?` citations and rejects `..` traversal, leading `/`, `L0`, `end < start`, and plain-text strings.
- [ ] **AC-2 (Progressive-Disclosure Summary & SHA Permalinks):** `format_evaluator_review_summary` and `post_evaluator_verdict` render the 1-line header, `🔴`/`🟡` severity badges, and `<details><summary>Learn more</summary>` blocks with `https://github.com/<tenant>/blob/<head_sha>/<path>#L<start>-L<end>` permalinks while keeping `parse(comment_body, ...)` round-trip intact.
- [ ] **AC-3 (Two-Stage Prompt Short-Circuit):** `EVALUATOR_SYSTEM_PROMPT` specifies Stage 1 static/contract short-circuit (`file:<path>#L<start>-L<end>`) before Stage 2 test/GUI verification.
- [ ] **AC-4 (CI-Parity Verification):** `uv run --no-sync atobar-ops verify` passes completely.
