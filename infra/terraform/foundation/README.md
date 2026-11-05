# Terraform Workspace: Foundation

Note: `.terraform.lock.hcl` files are intentionally ignored in this repository. We rely on HCP Terraform (remote) and VCS-driven workflows that trigger on commits to these respective workspace folders for provider version management.

## Network Security & Egress Rules (M06feat01)

The spot instances sandbox runs with a zero-inbound security group (`flow-spot-sg`) and restricted egress:
- **Port 443 (TCP)**: Allowed to `0.0.0.0/0` for outbound communication with GitHub API, LLM providers (Gemini, Anthropic), and the atobar-flow Control Panel.
- **Port 80 (TCP)**: Allowed for package mirrors and certificate revocation lists.
- **Port 53 (UDP/TCP)**: Allowed for DNS name resolution.
- **Unrestricted all-traffic (`-1`) egress is explicitly disabled.**
- **IMDSv2 Protection (`169.254.169.254`)**: IMDSv2 is enforced with hop-limit 1; host-level iptables rules reject access to `169.254.169.254` from unprivileged sandboxed agent processes to prevent credential exfiltration.

