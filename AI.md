# AI Agent Instructions — `atobar-flow` (Agentic Teams On BYOK And Repos)

If you are an AI assistant (Claude, GPT, Gemini, Cursor, Cline, Devin, `pi`, etc.) operating in this repository, read **`AGENTS.md`** in the repository root first.

**ATOBAR Initiative & Sibling Repositories (`~/projects/`):**
- **ATOBAR** stands for **"Agentic Teams On BYOK And Repos"**.
- **Main Factory Repository (`../atobar-flow`, this repo)** — The 5-package Python software factory (`contracts`, `flow-engine`, `control-panel`, `flow-agent`, `atobar-ops`).
- **Public Scrollytelling & Pitch Website ([`../atobar-flow.github.io`](../atobar-flow.github.io/AI.md))** — Static GitHub Pages site (`https://atobar-flow.github.io`, repo `atobar-flow/atobar-flow.github.io`). All public website changes happen in `../atobar-flow.github.io`.

**Context-Hygiene Mandate**:
- **DO NOT** run `cat docs/AGENTS.md docs/SOTA26_HEURISTICS.md docs/SWARM_ARCHITECTURE.md docs/ROADMAP.md` upfront.
- Query `docs/` on-demand using `rg -n "<term>" docs/` and read only the relevant lines (`limit <= 120`):
  1. **`docs/AGENTS.md`**: role boundaries (DEVELOPER / EVALUATOR / ARBITRATOR / CONDUCTOR), PR-comment handoff protocol, Definition of Done.
  2. **`docs/SOTA26_HEURISTICS.md`**: core architectural axioms.
  3. **`docs/SWARM_ARCHITECTURE.md`**: multi-tenant swarm topology, envelope schemas, tenant manifest, metric contract.
  4. **`docs/ROADMAP.md`**: current milestones (`M<xx>feat<yy>`) and issue workflow.

**Know your role.** At runtime, the factory injects `FLOW_ROLE` (`developer` | `evaluator` | `arbitrator`) and `FLOW_TENANT`. You act **only** within that role's boundary and communicate **only** through `flow-*` envelopes in the PR thread. If `FLOW_ROLE` is unset, you are a human-directed contributor to the factory itself: follow root `AGENTS.md` and use `rg -n` on `docs/` as needed.

**Secrets & Control Panel Endpoint Hygiene (MANDATORY — Fork-Safe Public Repo):**
- **Secrets over Variables**: All GitHub Actions deployment parameters (`AWS_REGION`, `AWS_PACKER_ROLE_ARN`, `AWS_DISPATCHER_ROLE_ARN`, `CP_HOSTNAME`, `CP_PUBLIC_URL`) MUST be stored as **GitHub Actions Secrets** (`secrets.*`, synced via `uv run --no-sync atobar-ops infra sync-vars --write`), NEVER as plaintext GitHub Actions Variables (`vars.*`).
- **Secret-Composed Control Panel Endpoint**: The Control Panel URL (`CP_PUBLIC_URL`) is composed dynamically at runtime from the `CP_HOSTNAME` / `CP_PUBLIC_URL` secret via `atobar_contracts.client_templates.resolve_cp_url()`. NEVER hardcode live or historical Control Panel domains, cloud account IDs, Supabase project refs, or TFC IDs in code, tests (use `https://cp.example.invalid`), docs, issues, or commit messages (enforced by `.gitleaks.toml` and `tests/test_boundaries.py`).

**Templates (mandatory):** before opening or updating a PR, fill in `.github/PULL_REQUEST_TEMPLATE.md` (including the Adversarial Self-Review corner-case and assumption audit). When reviewing a PR, follow `.github/REVIEW_TEMPLATE.md`.
