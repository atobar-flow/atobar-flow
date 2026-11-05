# Terraform Infrastructure

This directory contains the Infrastructure as Code (IaC) for the ATOBAR Flow project, managed via HCP Terraform.

## Workspace Alignment

The infrastructure is split into three distinct, decoupled workspaces in Terraform Cloud. Each workspace corresponds directly to a subdirectory here and operates independently.

| Directory | HCP Terraform Workspace | Workspace ID | Description |
| :--- | :--- | :--- | :--- |
| `foundation/` | `atobar-flow-foundations` | `ws-4QARuUjKHTqRztua` | Core network, VPC, Subnets, Security Groups, and IAM OIDC. |
| `identity/` | `atobar-flow-identity` | `ws-kLXELyPmbC4T4L1n` | Spot instance IAM roles, instance profiles, and SSM secrets definitions. |
| `control-panel/` | `atobar-flow-control-panel` | `ws-pQP9N2REWmJvekBs` | AWS Launch Template, Supabase secrets injection, and cloud-init definitions. |

*Note: Modifying files in any of these directories will automatically trigger a plan and apply for its respective workspace upon merging to `main` via VCS File Triggers.*

## Tooling & Local Usage

To interact with these workspaces locally, you need the Terraform CLI.

### Installation (macOS)

```bash
# Install Terraform
brew tap hashicorp/tap
brew install hashicorp/tap/terraform

# (Optional) Install tfenv for version management
brew install tfutils/tap/tfenv
```

### Usage

1. **Authenticate:** Ensure you are logged into HCP Terraform to access the remote state and run remote operations.
   ```bash
   terraform login
   ```
2. **Initialize:** Navigate into the specific workspace directory and initialize.
   ```bash
   cd infra/terraform/control-panel
   terraform init
   ```
3. **Plan / Apply:** Operations will run remotely in Terraform Cloud.
   ```bash
   terraform plan
   # If changes are acceptable and you want to bypass the automated VCS workflow (not recommended for production):
   terraform apply
   ```
