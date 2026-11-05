resource "aws_iam_openid_connect_provider" "github_actions" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["1b511abead59c6ce207077c0bf0e0043b1382612", "1c58a3a8518e8759bf075b76b750d4f2df264fcd"] # Standard GitHub OIDC thumbprints
}

resource "aws_iam_role" "gha_dispatcher" {
  name        = "flow-gha-dispatcher-role"
  description = "GitHub Actions OIDC role for supabase migrations (main branch only)"

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

data "aws_caller_identity" "current" {}

resource "aws_iam_role_policy" "gha_dispatcher_inline" {
  name = "flow-gha-dispatcher-inline-policy"
  role = aws_iam_role.gha_dispatcher.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:flow/supabase/credentials-*"
      }
    ]
  })
}
