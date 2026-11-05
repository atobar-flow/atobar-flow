# Hetzner Control Panel Terraform Workspace

HCP Terraform workspace (`atobar-flow-control-panel-hetzner`) provisioning the Hetzner Cloud `cx23` host, firewall, AWS IAM user/secrets for Control Panel, and pull-based systemd refresh timer.

## Break-Glass SSH Key (`~/.ssh/cp.key`)

`main.tf` generates an `ED25519` keypair (`tls_private_key.ssh`), registers the public key on Hetzner (`hcloud_ssh_key.cp`), and stores the private key in AWS Secrets Manager at `flow/control-panel/ssh_private_key`. `atobar-ops cp status` expects this key at `~/.ssh/cp.key` (`0600`):

```bash
(umask 077 && aws secretsmanager get-secret-value \
  --secret-id flow/control-panel/ssh_private_key \
  --region "${AWS_REGION:-us-west-2}" \
  --query SecretString --output text > ~/.ssh/cp.key)
```
