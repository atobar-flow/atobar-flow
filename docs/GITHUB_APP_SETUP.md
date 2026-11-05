# GitHub App Installation (Devin-like Experience)

To authorize the `flow` agent to securely access your repositories, we use a GitHub App instead of sharing personal access tokens (PATs). This ensures tokens are strictly scoped to the repositories you select and are short-lived, minimizing the blast radius (Axiom 3).

## For Users (Installing the App)

> Installing the App is stage 1 of onboarding. The full sequence, including the onboarding script, is at `https://$CP_HOSTNAME/onboarding` (maintainers: `docs/ONBOARDING.md`).

1. Navigate to the `atobar-flow` GitHub App installation page (URL provided by your administrator).
2. Click **Install**.
3. Choose the organization or personal account where your target repositories reside.
4. Select **Only select repositories** and choose the specific repositories you want the agent to access.
5. Click **Install & Authorize**.

Once installed, you can trigger the agent on any of the authorized repositories by adding the appropriate `flow:*` labels to issues. The agent will mint a short-lived (~1 hour) token specific to that repository.

## For Administrators (Publishing the App)

To publish the GitHub App and connect it to your Control Plane:

1. Go to your GitHub Organization -> **Settings** -> **GitHub Apps** -> **New GitHub App**.
2. Set a name (e.g., `atobar-flow-agent`).
3. Set the **Homepage URL** to your control plane repository.
4. Uncheck **Active** under Webhook (we use repository dispatch from the target repo, so the App itself doesn't need webhooks).
5. **Permissions:**
   - **Repository permissions:**
     - Contents: Read & Write
     - Issues: Read & Write
     - Pull Requests: Read & Write
     - Workflows: Read & Write
6. Create the App.
7. Generate a **Private Key** and download it.
8. Note the **App ID**.
9. Install the App on your own testing repositories to get an **Installation ID**.
10. Update the AWS SSM Parameter Store with your credentials:
    - `/flow/github/app_id` (String)
    - `/flow/github/app_installation_id` (String)
    - `/flow/github/app_private_key` (SecureString, paste the PEM file contents)

The `flow` agent will now automatically sign JWTs and exchange them for scoped installation tokens during the cloud-init boot sequence.
