data "aws_iam_policy_document" "assume_role_ec2" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "flow_spot_sandbox_role" {
  name               = "flow-spot-sandbox-role"
  assume_role_policy = data.aws_iam_policy_document.assume_role_ec2.json
}

data "aws_iam_policy_document" "spot_deny_secrets" {
  statement {
    effect = "Deny"
    actions = [
      "ssm:GetParameter*",
      "secretsmanager:*",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "spot_deny_secrets" {
  name   = "flow-spot-sandbox-deny-secrets"
  role   = aws_iam_role.flow_spot_sandbox_role.id
  policy = data.aws_iam_policy_document.spot_deny_secrets.json
}

resource "aws_iam_role_policy_attachment" "ssm_managed_core" {
  role       = aws_iam_role.flow_spot_sandbox_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "flow_spot_sandbox_profile" {
  name = "flow-spot-sandbox-profile"
  role = aws_iam_role.flow_spot_sandbox_role.name
}
