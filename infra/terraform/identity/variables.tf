variable "aws_region" {
  type        = string
  description = "The AWS Region"
  default     = "us-west-2"
}

variable "kms_deletion_window_days" {
  type        = number
  description = "Waiting period (7-30 days) before AWS KMS deletes the CMK on destroy"
  default     = 7
}

variable "secret_recovery_window_days" {
  type        = number
  description = "Recovery window in days (0 for immediate deletion on User-Zero reset, or 7-30)"
  default     = 0
}

variable "github_app_id" {
  type        = string
  description = "GitHub App ID for atobar-flow-agent"
  default     = "UNCONFIGURED"
}

variable "github_app_installation_id" {
  type        = string
  description = "Default GitHub App installation ID"
  default     = "UNCONFIGURED"
}

variable "github_app_private_key" {
  type        = string
  description = "PEM-encoded RSA private key for the GitHub App"
  sensitive   = true
  default     = "UNCONFIGURED"
}

variable "github_oauth_client_id" {
  type        = string
  description = "GitHub App OAuth client ID for Control Panel sign-in"
  default     = ""
}

variable "github_oauth_client_secret" {
  type        = string
  description = "GitHub App OAuth client secret for Control Panel sign-in"
  sensitive   = true
  default     = ""
}
