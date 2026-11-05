resource "aws_ebs_encryption_by_default" "main" {
  enabled = true
}

resource "aws_security_group" "spot" {
  name        = "flow-spot-sg"
  description = "Zero-inbound security group with restricted egress for role spot instances (M06feat01)"
  vpc_id      = aws_vpc.main.id

  # Allow HTTPS for GitHub API, LLM APIs (Gemini/Claude), and Control Panel
  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Allow HTTPS outbound for GitHub, LLM providers, and Control Panel"
  }

  # Allow HTTP for package mirrors and metadata endpoints
  egress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Allow HTTP outbound for package repositories and CRLs"
  }

  # Allow DNS resolution
  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Allow DNS UDP outbound"
  }

  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Allow DNS TCP outbound"
  }

  # Note on IMDS (169.254.169.254):
  # AWS IMDSv2 is enforced at the instance level via launch_template (http_tokens = "required", http_put_response_hop_limit = 1).
  # Direct access to 169.254.169.254/32 is restricted because IMDSv2 forbids requests with X-Forwarded-For, and Hop Limit is 1.
  # The agent's egress is restricted at the port level (80/443/53). Egress to specific domains is NOT enforced by this SG.

  tags = {
    Name = "flow-spot-sg"
  }
}
