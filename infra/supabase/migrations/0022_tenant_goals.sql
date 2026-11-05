-- 0022: Outer goal loop persistence table, run_credentials goal metadata, and atomic counter RPCs (#204).

create table if not exists public.tenant_goals (
  tenant_id uuid primary key references public.tenants(id) on delete cascade,
  goal_sha text not null,
  status text not null default 'open' check (status in ('open', 'met', 'stopped')),
  stop_reason text,
  spent_usd numeric(12,4) not null default 0,
  prs_merged integer not null default 0,
  merges_without_progress integer not null default 0,
  planning_run_id text,
  planning_started_at timestamptz,
  started_at timestamptz not null default now(),
  last_progress_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
alter table public.tenant_goals enable row level security;
grant select, insert, update, delete on public.tenant_goals to service_role;

alter table public.run_credentials
  add column if not exists goal_mode boolean not null default false,
  add column if not exists plan_count integer;

create or replace function public.goal_record_spend(
  p_tenant_id uuid,
  p_delta_usd numeric
) returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  update public.tenant_goals
     set spent_usd = round(spent_usd + greatest(0, p_delta_usd), 4),
         updated_at = now()
   where tenant_id = p_tenant_id
     and status = 'open';
end;
$$;
grant execute on function public.goal_record_spend(uuid, numeric) to service_role;

create or replace function public.goal_record_merge(
  p_tenant_id uuid,
  p_progress boolean
) returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  update public.tenant_goals
     set prs_merged = prs_merged + 1,
         merges_without_progress = case when p_progress then 0 else merges_without_progress + 1 end,
         last_progress_at = case when p_progress then now() else last_progress_at end,
         updated_at = now()
   where tenant_id = p_tenant_id
     and status = 'open';
end;
$$;
grant execute on function public.goal_record_merge(uuid, boolean) to service_role;
