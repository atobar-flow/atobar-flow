data "aws_ami_ids" "sandbox" {
  owners = ["self"]

  filter {
    name   = "name"
    values = ["flow-sandbox-v*"]
  }
}

data "aws_ami" "sandbox" {
  count       = length(data.aws_ami_ids.sandbox.ids) > 0 ? 1 : 0
  most_recent = true

  filter {
    name   = "name"
    values = ["flow-sandbox-v*"]
  }

  owners = ["self"]
}

data "aws_ami" "ubuntu_base" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"]
  }

  filter {
    name   = "root-device-type"
    values = ["ebs"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_launch_template" "flow_spot_sandbox" {
  name = "flow-spot-sandbox-template"

  image_id      = coalesce(try(data.aws_ami.sandbox[0].id, null), data.aws_ami.ubuntu_base.id)
  instance_type = "t4g.large"

  instance_initiated_shutdown_behavior = "terminate"

  iam_instance_profile {
    name = data.terraform_remote_state.identity.outputs.spot_instance_profile_name
  }

  network_interfaces {
    associate_public_ip_address = true
    subnet_id                   = data.terraform_remote_state.foundations.outputs.public_subnet_id
    security_groups             = [data.terraform_remote_state.foundations.outputs.spot_security_group_id]
  }

  instance_market_options {
    market_type = "spot"
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  user_data = base64encode(templatefile("${path.module}/scripts/cloud_init.sh.tpl", {
    aws_region   = var.aws_region
    factory_repo = var.factory_repo
  }))

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "flow-spot-sandbox"
    }
  }
}
