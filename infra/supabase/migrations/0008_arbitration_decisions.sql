-- Create arbitration_decisions table for persisting the ARBITRATOR's decision (Axiom 13)

create table if not exists public.arbitration_decisions (
  id              bigint generated always as identity primary key,
  tenant_id       uuid not null references public.tenants(id),
  pr_number       integer not null,
  head_sha        text not null check (head_sha ~ '^[0-9a-f]{40}$'),
  mode            text not null check (mode in ('shadow', 'recommend', 'auto_merge')),
  decision        text not null,
  reasons         text,
  created_at      timestamptz not null default now()
);

create index if not exists arbitration_decisions_tenant_pr on public.arbitration_decisions (tenant_id, pr_number);

alter table public.arbitration_decisions enable row level security;

-- Only service_role can read/write for now
create policy "service_role_all" on public.arbitration_decisions for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

-- M02feat04 requires explicit grants for PostgREST
grant select, insert, update, delete on table public.arbitration_decisions to service_role;
