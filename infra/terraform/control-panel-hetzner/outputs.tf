output "control_panel_url" {
  value     = "https://${var.cp_hostname}"
  sensitive = true
}

output "server_ipv4" {
  value = hcloud_server.cp.ipv4_address
}

output "admin_token_secret_name" {
  value = aws_secretsmanager_secret.admin_token.name
}

resource "aws_ssm_parameter" "cp_url" {
  name  = "/flow/control-panel/url"
  type  = "SecureString"
  value = "https://${var.cp_hostname}"
}
