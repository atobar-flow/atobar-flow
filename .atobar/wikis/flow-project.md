## Purpose
Autonomous AI SWE execution infrastructure. Provisions ephemeral AWS Spot instances
that run a Gemini-powered ReAct agent loop to solve GitHub issues. The agent has
deterministic tools (MultiEdit, exec, ripgrep, vision) and strict circuit breakers.

## Key Technologies
- Terraform (HCP Cloud backend, AWS provider ~>5.0)
- Packer (Amazon EBS builder, Ubuntu 24.04 ARM64)
- Python 3.12 (asyncio, pty module, Pydantic)
- Google Gemini API (google-genai SDK, Context Caching, Function Calling)
- AWS (EC2 Spot, IAM OIDC, SSM Parameter Store, Secrets Manager)
- GitHub Actions (OIDC dispatcher, CI pipeline)
- Supabase (PostgreSQL state store, free tier)
- Systemd (Xtigervnc, websockify, openbox, chromium)

## Top-Level Structure
- `infra/terraform/foundation/` — VPC, subnets, security groups, OIDC IAM (WS: `ws-4QARuUjKHTqRztua`)
- `infra/terraform/identity/` — Spot instance role, SSM secrets, instance profile (WS: `ws-kLXELyPmbC4T4L1n`)
- `infra/terraform/control-panel/` — Launch template, Supabase secrets, cloud-init (WS: `ws-pQP9N2REWmJvekBs`)
- `infra/packer/` — AMI definition, systemd services, install scripts
- `src/flow_engine/tools/` — Agent tools (exec, fs, search, vision, ci, state)
- `src/flow_engine/agent/` — ReAct loop, context manager, tool registry
- `src/flow_engine/telemetry/` — GitHub Issue markdown sidecar
- `src/flow_engine/core/` — Circuit breakers, watchdog
- `.atobar/` — Phase 0 bootstrapper, wikis (this directory)
- `tests/` — Pytest unit tests mirroring src/ layout
- `docs/` — SOTA26_HEURISTICS.md (ADR/manifesto)

## Key Concepts
- **Optimistic Lock:** MultiEdit rejects edits if old_string is not globally unique
- **Head Truncation:** exec output capped at 4000 chars, overflow dumped to /tmp/
- **Zero-Blast-Radius Auth:** GitHub token fetched from SSM at boot, never in agent context
- **Anti-RAG:** No VectorDBs — deterministic ripgrep + static wiki injection
- **Phase 0:** cloud-init runs before agent wakes — venv, deps, git config
- **Stateless Recovery:** Git-based WIP branch synced every 60s for Spot preemption
- **Circuit Breakers:** OS-level kill after 3 CI failures or 60min wall-clock
- **Done Gate:** Remote GitHub Actions CI is the only completion signal
- **SOTA YOLO:** Multimodal vision via Xtigervnc framebuffer + CDP DOM annotations