packer {
  required_plugins {
    amazon = {
      version = "~> 1.0"
      source  = "github.com/hashicorp/amazon"
    }
  }
}

variable "vpc_id" {
  type    = string
  default = null
}

variable "subnet_id" {
  type    = string
  default = null
}

variable "aws_region" {
  type    = string
  default = "us-west-2"
}

source "amazon-ebs" "ubuntu" {
  ami_name      = "flow-sandbox-v{{timestamp}}"
  instance_type = "t4g.large" # ARM64 instance
  region        = var.aws_region
  ssh_username  = "ubuntu"
  vpc_id        = var.vpc_id
  subnet_id     = var.subnet_id

  source_ami_filter {
    filters = {
      name                = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    most_recent = true
    owners      = ["099720109477"] # Canonical
  }
}

build {
  name = "flow-sandbox"
  sources = [
    "source.amazon-ebs.ubuntu"
  ]

  provisioner "shell" {
    script = "scripts/install_deps.sh"
  }

  provisioner "file" {
    source      = "files/vncserver.service"
    destination = "/tmp/vncserver.service"
  }

  provisioner "file" {
    source      = "files/vncproxy.service"
    destination = "/tmp/vncproxy.service"
  }

  provisioner "file" {
    source      = "files/chrome.service"
    destination = "/tmp/chrome.service"
  }

  provisioner "file" {
    source      = "files/openbox.service"
    destination = "/tmp/openbox.service"
  }

  provisioner "shell" {
    inline = [
      "sudo mv /tmp/vncserver.service /etc/systemd/system/vncserver.service",
      "sudo mv /tmp/vncproxy.service /etc/systemd/system/vncproxy.service",
      "sudo mv /tmp/chrome.service /etc/systemd/system/chrome.service",
      "sudo mv /tmp/openbox.service /etc/systemd/system/openbox.service",
      "sudo systemctl daemon-reload",
      "sudo systemctl enable vncserver openbox vncproxy chrome"
    ]
  }
}