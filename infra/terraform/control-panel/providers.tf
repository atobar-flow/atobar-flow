terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  cloud {
    workspaces {
      name = "atobar-flow-control-panel"
    }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project   = "flow"
      ManagedBy = "terraform"
      Layer     = "control-panel"
    }
  }
}

data "terraform_remote_state" "foundations" {
  backend = "remote"

  config = {
    organization = var.tfc_organization_name
    workspaces = {
      name = var.tfc_foundations_workspace_name
    }
  }
}

data "terraform_remote_state" "identity" {
  backend = "remote"

  config = {
    organization = var.tfc_organization_name
    workspaces = {
      name = var.tfc_identity_workspace_name
    }
  }
}
