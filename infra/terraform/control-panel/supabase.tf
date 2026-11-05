resource "aws_secretsmanager_secret" "supabase_credentials" {
  name                    = "flow/supabase/credentials"
  description             = "Supabase connection credentials (API URL, Service Key, DB Password, Pooler Host/Port/User)"
  recovery_window_in_days = var.secret_recovery_window_days
}

resource "aws_secretsmanager_secret_version" "supabase_credentials" {
  secret_id = aws_secretsmanager_secret.supabase_credentials.id
  secret_string = jsonencode({
    api_url     = var.supabase_api_url
    service_key = var.supabase_service_key
    db_password = var.supabase_db_password
    pghost      = var.supabase_pghost
    pgport      = var.supabase_pgport
    pguser      = var.supabase_pguser
  })
}
