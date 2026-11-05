-- 0006 run credentials: single-use credentials for delivering tenant LLM keys (#151)
create table if not exists public.run_credentials (
  run_id      text primary key,
  tenant_id   uuid not null references public.tenants(id) on delete cascade,
  role        text not null,
  cred_hash   text not null,
  expires_at  timestamptz not null,
  used_at     timestamptz,
  created_at  timestamptz not null default now()
);

create index if not exists run_credentials_tenant on public.run_credentials (tenant_id);

alter table public.run_credentials enable row level security;
grant select, insert, update on public.run_credentials to service_role;
