resource "aws_ssm_parameter" "github_agent_token" {
  name  = "/flow/github/agent_token"
  type  = "SecureString"
  value = "DEPRECATED_USE_GITHUB_APP"
}

resource "aws_ssm_parameter" "github_app_id" {
  name  = "/flow/github/app_id"
  type  = "String"
  value = nonsensitive(var.github_app_id != "" ? var.github_app_id : "UNCONFIGURED")
}

resource "aws_ssm_parameter" "github_app_installation_id" {
  name  = "/flow/github/app_installation_id"
  type  = "String"
  value = nonsensitive(var.github_app_installation_id != "" ? var.github_app_installation_id : "UNCONFIGURED")
}

resource "aws_ssm_parameter" "github_app_private_key" {
  name  = "/flow/github/app_private_key"
  type  = "SecureString"
  value = var.github_app_private_key != "" ? var.github_app_private_key : "UNCONFIGURED"
}

resource "aws_ssm_parameter" "gemini_api_key" {
  name  = "/flow/gemini/api_key"
  type  = "SecureString"
  value = "DEPRECATED_USE_BYOK_KMS"
}

resource "aws_secretsmanager_secret" "github_oauth" {
  name                    = "flow/control-panel/github_oauth"
  description             = "GitHub OAuth client credentials for Control Panel browser sign-in"
  recovery_window_in_days = var.secret_recovery_window_days
}

resource "aws_secretsmanager_secret_version" "github_oauth" {
  secret_id = aws_secretsmanager_secret.github_oauth.id
  secret_string = jsonencode({
    client_id     = var.github_oauth_client_id
    client_secret = var.github_oauth_client_secret
  })
}
