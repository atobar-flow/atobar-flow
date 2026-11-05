variable "aws_region" {
  type    = string
  default = "us-west-2"
}

variable "server_type" {
  type    = string
  default = "cx23"
}

variable "location" {
  type    = string
  default = "fsn1"
}

variable "ssh_allowed_cidrs" {
  description = "Break-glass SSH sources. Empty = port 22 closed."
  type        = list(string)
  default     = []
}

variable "factory_repo" {
  type    = string
  default = "atobar-flow/atobar-flow"
}

variable "git_ref" {
  description = "atobar-flow ref the server tracks for deployment."
  type        = string
  default     = "main"
}

variable "cp_hostname" {
  description = "Public hostname for the Control Panel (sensitive secret); Caddy obtains a Let's Encrypt certificate for it. Needs a DNS A record to the server IPv4."
  type        = string
  sensitive   = true
}

variable "cp_admin_logins" {
  description = "GitHub logins that can see every tenant in the Control Panel UI."
  type        = list(string)
  default     = ["arsenyspb"]
}

variable "secret_recovery_window_days" {
  description = "Recovery window in days (0 for immediate deletion on User-Zero reset, or 7-30)"
  type        = number
  default     = 0
}

variable "ghcr_image_prefix" {
  description = "GHCR repository owner prefix for pre-built Control Panel and Flow Engine images"
  type        = string
  default     = "ghcr.io/atobar-flow"
}
