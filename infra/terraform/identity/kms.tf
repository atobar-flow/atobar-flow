resource "aws_kms_key" "tenant_llm_cmk" {
  description             = "Customer-managed KMS key for envelope encryption of tenant BYOK LLM API keys"
  deletion_window_in_days = var.kms_deletion_window_days
  enable_key_rotation     = true
}

resource "aws_kms_alias" "tenant_llm_cmk" {
  name          = "alias/flow-tenant-llm-cmk"
  target_key_id = aws_kms_key.tenant_llm_cmk.key_id
}
