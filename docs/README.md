# Product, Architecture & Operations Documentation

| Document | Purpose |
| :--- | :--- |
| [`PRD.md`](PRD.md) | Canonical Product Requirements Document: BYOK cost sharing & routing (per-repo, per-role today; per-invoking-user on roadmap), personas (`P1`–`P4`), user journeys (`J1`–`J6`), requirements (`FR-1`..`FR-7`, `NFR-1`..`NFR-4`), and the `https://github.com/atobar-flow` scrollytelling storyboard. |
| [`HLD.md`](HLD.md) | Canonical High-Level Design derived from `PRD.md`: Hub-and-Spoke Mermaid diagrams, runtime sequences, single-writer table ownership, and `[VISION]` blueprints. |
| [`SWARM_ARCHITECTURE.md`](SWARM_ARCHITECTURE.md) | 4-role stigmergic protocol (`flow-plan`, `flow-ack`, `flow-verdict`, `flow-arbitration`), failure-mode defenses, and 5-package codebase layout. |
| [`ONBOARDING.md`](ONBOARDING.md) | 8-stage client repository onboarding (`onboard.sh`), metric specs, `@atobar-flow-agent` conversational steering, and `SotaWiki` auto-refresh. |
| [`ROADMAP.md`](ROADMAP.md) | As-built feature table (`M01`–`M11` and `J34`–`J51`) and milestone history. |
| [`E2E_RUNBOOK.md`](E2E_RUNBOOK.md) | Operator runbook for E2E verification on `ollama-monitor` via `atobar-ops`. |
| [`CROSS_SERVICE_CHANGES.md`](CROSS_SERVICE_CHANGES.md) | Mechanical wiring rules across `contracts`, `flow-engine`, `control-panel`, and Spot `UserData`. |
| [`AGENTS.md`](AGENTS.md) | Role boundaries and contributor rules for AI coding agents. |
