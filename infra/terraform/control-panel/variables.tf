variable "tfc_organization_name" {
  type        = string
  description = "The name of the HCP Terraform organization"
  default     = ""
}

variable "tfc_workspace_name" {
  type        = string
  description = "The name of the HCP Terraform workspace"
  default     = "atobar-flow-control-panel"
}

variable "tfc_foundations_workspace_name" {
  type        = string
  description = "The name of the HCP Terraform foundations workspace"
  default     = "atobar-flow-foundations"
}

variable "tfc_identity_workspace_name" {
  type        = string
  description = "The name of the HCP Terraform identity workspace"
  default     = "atobar-flow-identity"
}

variable "factory_repo" {
  type        = string
  description = "The GitHub owner/repo of the Atobar Flow factory"
  default     = "atobar-flow/atobar-flow"
}

variable "secret_recovery_window_days" {
  type        = number
  description = "Recovery window in days (0 for immediate deletion on User-Zero reset, or 7-30)"
  default     = 0
}

variable "supabase_api_url" {
  type        = string
  description = "The REST API URL for the Supabase project"
  sensitive   = true
}

variable "supabase_service_key" {
  type        = string
  description = "The service role key for the Supabase project"
  sensitive   = true
}

variable "supabase_db_password" {
  type        = string
  description = "The database password for the Supabase project"
  sensitive   = true
}

variable "supabase_pghost" {
  type        = string
  description = "The Supavisor session pooler hostname"
  sensitive   = true
  default     = ""
}

variable "supabase_pgport" {
  type        = string
  description = "The Supavisor session pooler port"
  default     = "5432"
}

variable "supabase_pguser" {
  type        = string
  description = "The Supavisor session pooler user (postgres.<project-ref>)"
  default     = ""
}

variable "aws_region" {
  type        = string
  description = "The AWS Region"
  default     = "us-west-2"
}
