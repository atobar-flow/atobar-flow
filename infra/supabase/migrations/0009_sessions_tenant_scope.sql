-- Expand sessions table for Axiom 10 multi-tenant isolation

alter table public.sessions
  add column if not exists tenant_id uuid references public.tenants(id),
  add column if not exists role text,
  add column if not exists run_id text;

-- Insert/Update only allowed for the specific instance ID
alter table public.sessions enable row level security;

-- Maintain service role full access
create policy "service_role_all" on public.sessions for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

-- M02feat04 requires explicit grants for PostgREST
grant select, insert, update, delete on table public.sessions to service_role;
