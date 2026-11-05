-- 0018: allow canonical provider IDs ('claude', 'grok', 'openai') in tenant_llm_keys.provider.
-- 0004 constrained provider to ('gemini','anthropic','deepseek','xai'), while Control Panel
-- and flow_engine canonicalized Anthropic -> 'claude' and xAI -> 'grok'.
alter table public.tenant_llm_keys
  drop constraint if exists tenant_llm_keys_provider_check,
  add constraint tenant_llm_keys_provider_check
    check (provider in ('gemini', 'claude', 'anthropic', 'deepseek', 'grok', 'xai', 'openai'));
