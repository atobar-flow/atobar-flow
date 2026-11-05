terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  cloud {
    workspaces {
      name = "atobar-flow-foundations"
    }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project   = "flow"
      ManagedBy = "terraform"
    }
  }
}
