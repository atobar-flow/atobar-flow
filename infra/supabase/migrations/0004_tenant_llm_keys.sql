-- 0004: bring-your-own LLM keys (#151). The Control Panel encrypts each key with
-- AES-256-GCM under a KEK held in AWS SM (flow/control-panel/llm_kek) before it
-- reaches this table; Supabase only ever sees ciphertext and the last 4 chars.
create table if not exists public.tenant_llm_keys (
  tenant_id    uuid not null references public.tenants(id) on delete cascade,
  provider     text not null check (provider in ('gemini','anthropic','deepseek','xai')),
  ciphertext   text not null,
  kek_version  integer not null default 1,
  key_last4    text not null check (char_length(key_last4) <= 4),
  validated_at timestamptz not null,
  created_by   text not null,
  updated_at   timestamptz not null default now(),
  primary key (tenant_id, provider)
);

alter table public.tenant_llm_keys enable row level security;
grant select, insert, update, delete on public.tenant_llm_keys to service_role;
