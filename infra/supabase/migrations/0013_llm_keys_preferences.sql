-- 0013: add preferences to tenant_llm_keys for model and thinking selection
alter table public.tenant_llm_keys
add column selected_model text,
add column thinking_level text;