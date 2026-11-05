-- 0012: add available_models to tenant_llm_keys to expose model list in UI
alter table public.tenant_llm_keys
add column available_models jsonb;