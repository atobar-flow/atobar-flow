output "spot_instance_profile_name" {
  value       = aws_iam_instance_profile.flow_spot_sandbox_profile.name
  description = "Name of the IAM instance profile for the Spot instance"
}

output "spot_instance_profile_arn" {
  value       = aws_iam_instance_profile.flow_spot_sandbox_profile.arn
  description = "ARN of the IAM instance profile for the Spot instance"
}

output "spot_sandbox_role_id" {
  value       = aws_iam_role.flow_spot_sandbox_role.id
  description = "The ID (name) of the Spot Sandbox Role"
}

output "github_token_ssm_path" {
  value       = aws_ssm_parameter.github_agent_token.name
  description = "The SSM Parameter path for the GitHub agent token"
}

output "github_app_id_ssm_path" {
  value       = aws_ssm_parameter.github_app_id.name
  description = "The SSM Parameter path for the GitHub App ID"
}

output "github_app_installation_id_ssm_path" {
  value       = aws_ssm_parameter.github_app_installation_id.name
  description = "The SSM Parameter path for the GitHub App Installation ID"
}

output "github_app_private_key_ssm_path" {
  value       = aws_ssm_parameter.github_app_private_key.name
  description = "The SSM Parameter path for the GitHub App Private Key"
}

output "gemini_api_key_ssm_path" {
  value       = aws_ssm_parameter.gemini_api_key.name
  description = "The SSM Parameter path for the Gemini API key"
}

output "github_oauth_secret_arn" {
  value       = aws_secretsmanager_secret.github_oauth.arn
  description = "The ARN of the GitHub OAuth Secrets Manager secret"
}
