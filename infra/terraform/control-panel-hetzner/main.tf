data "aws_caller_identity" "current" {}

data "aws_kms_alias" "tenant_llm_cmk" {
  name = "alias/flow-tenant-llm-cmk"
}

locals {
  account = data.aws_caller_identity.current.account_id
}

# --- Admin token: plaintext only in AWS SM (read by onboard-tenant.yml); the server gets its sha256.
resource "random_password" "admin_token" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "admin_token" {
  name                    = "flow/control-panel/admin_token"
  description             = "Control Panel admin bearer token (tenant registration / token issuance)"
  recovery_window_in_days = var.secret_recovery_window_days
}

resource "aws_secretsmanager_secret_version" "admin_token" {
  secret_id     = aws_secretsmanager_secret.admin_token.id
  secret_string = random_password.admin_token.result
}

# --- GitHub sign-in: session-cookie signing key and admin logins. The OAuth client
# secret itself (flow/control-panel/github_oauth) is created by a human, not Terraform.
resource "random_password" "session_secret" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "session_secret" {
  name                    = "flow/control-panel/session_secret"
  description             = "HMAC key for Control Panel browser session cookies"
  recovery_window_in_days = var.secret_recovery_window_days
}

resource "aws_secretsmanager_secret_version" "session_secret" {
  secret_id     = aws_secretsmanager_secret.session_secret.id
  secret_string = random_password.session_secret.result
}

# --- BYOK LLM keys: 256-bit KEK; tenant keys are AES-GCM-encrypted with it before
# reaching Supabase (control-panel/llm_keys.py). Never rotate by replacement:
# existing ciphertext would become unreadable.
resource "random_id" "llm_kek" {
  byte_length = 32
}

resource "aws_secretsmanager_secret" "llm_kek" {
  name                    = "flow/control-panel/llm_kek"
  description             = "Key-encryption key for tenant LLM API keys (base64, 32 bytes)"
  recovery_window_in_days = var.secret_recovery_window_days
}

resource "aws_secretsmanager_secret_version" "llm_kek" {
  secret_id     = aws_secretsmanager_secret.llm_kek.id
  secret_string = random_id.llm_kek.b64_std
}

resource "aws_ssm_parameter" "admin_logins" {
  name  = "/flow/control-panel/admin_logins"
  type  = "String"
  value = join(",", var.cp_admin_logins)
}

# --- Break-glass SSH key, private half kept only in AWS SM (never in git).
resource "tls_private_key" "ssh" {
  algorithm = "ED25519"
}

resource "aws_secretsmanager_secret" "ssh_key" {
  name                    = "flow/control-panel/ssh_private_key"
  description             = "Break-glass SSH key for the Hetzner Control Panel server"
  recovery_window_in_days = var.secret_recovery_window_days
}

resource "aws_secretsmanager_secret_version" "ssh_key" {
  secret_id     = aws_secretsmanager_secret.ssh_key.id
  secret_string = tls_private_key.ssh.private_key_openssh
}

resource "hcloud_ssh_key" "cp" {
  name       = "atobar-flow-control-panel"
  public_key = tls_private_key.ssh.public_key_openssh
}

# --- Least-privilege AWS identity for the server to pull runtime secrets at boot.
resource "aws_iam_user" "cp" {
  name = "flow-control-panel-hetzner"
}

resource "aws_iam_user_policy" "cp" {
  name = "flow-control-panel-read-secrets"
  user = aws_iam_user.cp.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["ssm:GetParameter"]
        Resource = [
          "arn:aws:ssm:${var.aws_region}:${local.account}:parameter/flow/github/app_id",
          "arn:aws:ssm:${var.aws_region}:${local.account}:parameter/flow/github/app_installation_id",
          "arn:aws:ssm:${var.aws_region}:${local.account}:parameter/flow/github/app_private_key",
          aws_ssm_parameter.admin_logins.arn,
        ]
      },
      {
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          "arn:aws:secretsmanager:${var.aws_region}:${local.account}:secret:flow/supabase/credentials-*",
          "arn:aws:secretsmanager:${var.aws_region}:${local.account}:secret:flow/control-panel/github_oauth-*",
          aws_secretsmanager_secret.session_secret.arn,
          aws_secretsmanager_secret.llm_kek.arn,
          aws_secretsmanager_secret.kms_aws_env.arn,
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource = data.aws_kms_alias.tenant_llm_cmk.target_key_arn
        Condition = {
          Null = {
            "kms:EncryptionContext:tenant_id" = "false"
            "kms:EncryptionContext:provider"  = "false"
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:DescribeLaunchTemplateVersions",
          "ec2:DescribeInstances",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:RunInstances",
          "ec2:CreateTags",
        ]
        Resource = [
          "arn:aws:ec2:${var.aws_region}:*:instance/*",
          "arn:aws:ec2:${var.aws_region}:*:spot-instances-request/*",
          "arn:aws:ec2:${var.aws_region}:*:subnet/*",
          "arn:aws:ec2:${var.aws_region}:*:security-group/*",
          "arn:aws:ec2:${var.aws_region}:*:network-interface/*",
          "arn:aws:ec2:${var.aws_region}:*:volume/*",
          "arn:aws:ec2:${var.aws_region}:*:image/*",
          "arn:aws:ec2:${var.aws_region}:*:key-pair/*",
          "arn:aws:ec2:${var.aws_region}:*:launch-template/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:TerminateInstances"]
        Resource = "arn:aws:ec2:${var.aws_region}:${local.account}:instance/*"
        Condition = {
          Null = {
            "aws:ResourceTag/Tenant" = "false"
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = "arn:aws:iam::${local.account}:role/flow-spot-sandbox-role"
      },
    ]
  })
}

resource "aws_iam_access_key" "cp" {
  user = aws_iam_user.cp.name
}

# --- KMS-only IAM identity mounted into the internet-facing `api` container (`aws-kms.env`, J43 WP-4).
resource "aws_iam_user" "cp_kms" {
  name = "flow-control-panel-kms"
}

resource "aws_iam_user_policy" "cp_kms" {
  name = "flow-control-panel-kms-envelope"
  user = aws_iam_user.cp_kms.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource = data.aws_kms_alias.tenant_llm_cmk.target_key_arn
        Condition = {
          Null = {
            "kms:EncryptionContext:tenant_id" = "false"
            "kms:EncryptionContext:provider"  = "false"
          }
        }
      },
    ]
  })
}

resource "aws_iam_access_key" "cp_kms" {
  user = aws_iam_user.cp_kms.name
}

resource "aws_secretsmanager_secret" "kms_aws_env" {
  name                    = "flow/control-panel/kms_aws_env"
  description             = "KMS-only AWS credentials for the Control Panel API container (aws-kms.env)"
  recovery_window_in_days = var.secret_recovery_window_days
}

resource "aws_secretsmanager_secret_version" "kms_aws_env" {
  secret_id     = aws_secretsmanager_secret.kms_aws_env.id
  secret_string = "AWS_ACCESS_KEY_ID=${aws_iam_access_key.cp_kms.id}\nAWS_SECRET_ACCESS_KEY=${aws_iam_access_key.cp_kms.secret}\nAWS_DEFAULT_REGION=${var.aws_region}\nAWS_REGION=${var.aws_region}"
}

# --- Server
resource "hcloud_firewall" "cp" {
  name = "atobar-flow-control-panel"

  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "80"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "443"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  dynamic "rule" {
    for_each = length(var.ssh_allowed_cidrs) > 0 ? [1] : []
    content {
      direction  = "in"
      protocol   = "tcp"
      port       = "22"
      source_ips = var.ssh_allowed_cidrs
    }
  }
}

# Stable public IPv4 so the cp_hostname DNS A record survives server rebuilds.
# Reserved IPv4 that cp_hostname points at; outlives server rebuilds (auto_delete = false).
resource "hcloud_primary_ip" "cp" {
  name        = "atobar-flow-control-panel"
  type        = "ipv4"
  location    = var.location
  auto_delete = false
  labels = {
    project = "atobar-flow"
    role    = "control-panel"
  }
}

resource "hcloud_server" "cp" {
  name         = "atobar-flow-control-panel"
  image        = "ubuntu-24.04"
  server_type  = var.server_type
  location     = var.location
  ssh_keys     = [hcloud_ssh_key.cp.id]
  firewall_ids = [hcloud_firewall.cp.id]

  public_net {
    ipv4_enabled = true
    ipv4         = hcloud_primary_ip.cp.id
    ipv6_enabled = true
  }

  user_data = templatefile("${path.module}/scripts/cloud_init.sh.tpl", {
    aws_region                = var.aws_region
    aws_access_key_id         = aws_iam_access_key.cp.id
    aws_secret_access_key     = aws_iam_access_key.cp.secret
    kms_aws_access_key_id     = aws_iam_access_key.cp_kms.id
    kms_aws_secret_access_key = aws_iam_access_key.cp_kms.secret
    admin_token_sha256        = sha256(random_password.admin_token.result)
    factory_repo              = var.factory_repo
    git_ref                   = var.git_ref
    cp_hostname               = var.cp_hostname
    ghcr_image_prefix         = var.ghcr_image_prefix
  })

  # Deploy logic is pulled from git by infra/control-panel/refresh.sh; bootstrap
  # edits must not replace the server (and its Caddy certificate volume).
  lifecycle {
    ignore_changes = [user_data]
  }

  labels = {
    project = "atobar-flow"
    role    = "control-panel"
  }
}
