terraform {
  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.45"
    }
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
  cloud {
    workspaces {
      name = "atobar-flow-control-panel-hetzner"
    }
  }
}

# HCLOUD_TOKEN and AWS_* come from the `atobar-broadcast` variable set (project-wide).
provider "hcloud" {}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project   = "flow"
      ManagedBy = "terraform"
      Layer     = "control-panel-hetzner"
    }
  }
}
