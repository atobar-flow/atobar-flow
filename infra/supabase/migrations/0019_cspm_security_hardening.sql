alter table public.tenant_llm_keys
  add column if not exists wrapped_dek text,
  add column if not exists kms_key_id text,
  add column if not exists encryption_mode text not null default 'local_kek'
    check (encryption_mode in ('local_kek', 'kms_envelope'));

alter table public.run_credentials
  add column if not exists claimed_instance_id text;

create table if not exists public.security_audit_log (
  id bigserial primary key,
  tenant_id uuid references public.tenants(id),
  event_type text not null,
  actor text not null,
  run_id text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

alter table public.security_audit_log enable row level security;
drop trigger if exists security_audit_log_immutable on public.security_audit_log;
create trigger security_audit_log_immutable
  before update or delete on public.security_audit_log
  for each row execute function public.flow_reject_mutation();

grant select, insert on public.security_audit_log to service_role;
grant usage, select on sequence public.security_audit_log_id_seq to service_role;
