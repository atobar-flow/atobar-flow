# Terraform Workspace: Identity

Note: `.terraform.lock.hcl` files are intentionally ignored in this repository. We rely on HCP Terraform (remote) and VCS-driven workflows that trigger on commits to these respective workspace folders for provider version management.

## Role Identities & Isolation (M04feat01)

The system uses a single unified GitHub App (`atobar-flow-agent`) to authenticate for all roles. Isolation is enforced through `FLOW_RUN_ID` runtime correlations against central CP records rather than IAM boundaries.

Note: The `/flow/apps/*` SSM parameter namespace is deprecated and will be deleted in a future update.

### SSM Parameters
- `/flow/github/app_private_key` (SecureString): GitHub App private key PEM
- `/flow/github/app_id` (String): GitHub App ID
- `/flow/github/app_installation_id` (String): Default GitHub App Installation ID (fallback if JWT-based discovery fails)

### Instance Profiles
- `flow-spot-sandbox-profile` -> role `flow-spot-sandbox-role`
