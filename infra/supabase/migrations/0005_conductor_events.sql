-- 0005 conductor events: Track processed events for flow_conductor idempotency (Axiom 12, #166).

create table if not exists public.conductor_events (
  event_id   text not null,
  tenant_id  uuid not null references public.tenants(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (tenant_id, event_id)
);

create index if not exists conductor_events_tenant on public.conductor_events (tenant_id);

alter table public.conductor_events enable row level security;
grant select, insert on public.conductor_events to service_role;
