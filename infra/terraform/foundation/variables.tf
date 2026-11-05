variable "aws_region" {
  type        = string
  description = "The AWS Region"
  default     = "us-west-2"
}

variable "factory_repo" {
  type        = string
  description = "The GitHub owner/repo of the Atobar Flow factory"
  default     = "atobar-flow/atobar-flow"
}
