resource "aws_iam_role" "gha_packer" {
  name = "flow-gha-packer-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRoleWithWebIdentity"
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.github_actions.arn
        }
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          }
          StringLike = {
            # TIGHTER than dispatcher: only main branch, not all refs (supports GitHub immutable OIDC sub)
            "token.actions.githubusercontent.com:sub" = [
              "repo:${var.factory_repo}:ref:refs/heads/main",
              "repo:${split("/", var.factory_repo)[0]}@*/${split("/", var.factory_repo)[1]}@*:ref:refs/heads/main",
            ]
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "gha_packer_inline" {
  name = "flow-gha-packer-inline-policy"
  role = aws_iam_role.gha_packer.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "PackerBuildPermissions"
        Effect = "Allow"
        Action = [
          "ec2:DescribeImages",
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceStatus",
          "ec2:DescribeRegions",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeSubnets",
          "ec2:DescribeVolumes",
          "ec2:DescribeVpcs",
          "ec2:DescribeTags",
          "ec2:DescribeSnapshots",
          "ec2:CreateImage",
          "ec2:RegisterImage",
          "ec2:CreateKeypair",
          "ec2:DeleteKeypair",
          "ec2:RunInstances",
          "ec2:TerminateInstances",
          "ec2:StopInstances",
          "ec2:CreateTags",
          "ec2:CreateSecurityGroup",
          "ec2:AuthorizeSecurityGroupIngress",
          "ec2:DeleteSecurityGroup",
          "ec2:CreateSnapshot",
          "ec2:GetPasswordData",
          "ec2:ModifyImageAttribute"
        ]
        Resource = "*"
      },
      {
        Sid    = "PackerAMILifecycle"
        Effect = "Allow"
        Action = [
          "ec2:DeregisterImage",
          "ec2:DeleteSnapshot"
        ]
        Resource = "*"
      }
    ]
  })
}