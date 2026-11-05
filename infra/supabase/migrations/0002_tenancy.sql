-- 0002 tenancy: Client Repo = tenant (docs/SWARM_ARCHITECTURE.md §3).
-- Only the service role (Control Panel API / factory) touches these tables;
-- RLS is enabled with no anon/authenticated policies, so PostgREST public
-- keys see nothing.

create table if not exists public.tenants (
  id                 uuid primary key default gen_random_uuid(),
  repo               text not null unique check (repo ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  default_branch     text not null default 'main',
  status             text not null default 'onboarding'
                     check (status in ('onboarding','active','paused','offboarded')),
  manifest           jsonb not null,
  manifest_sha       text not null,
  arbitration_mode   text not null default 'shadow'
                     check (arbitration_mode in ('shadow','recommend','auto_merge')),
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

create table if not exists public.metric_definitions (
  tenant_id     uuid not null references public.tenants(id) on delete cascade,
  key           text not null check (key ~ '^[a-z][a-z0-9_]*$'),
  source        jsonb not null,
  comparator    text not null check (comparator in ('>=','<=','>','<','==')),
  threshold     double precision not null,
  baseline_mode text not null default 'absolute'
                check (baseline_mode in ('absolute','no_regression')),
  min_samples   integer not null default 1 check (min_samples >= 1),
  updated_at    timestamptz not null default now(),
  primary key (tenant_id, key)
);

create table if not exists public.metric_observations (
  id              bigint generated always as identity primary key,
  tenant_id       uuid not null references public.tenants(id),
  key             text not null,
  value           double precision not null,
  commit_sha      text not null check (commit_sha ~ '^[0-9a-f]{40}$'),
  pr_number       integer,
  check_run_id    bigint,
  workflow_path   text not null,
  run_attempt     integer,
  artifact_sha256 text not null check (artifact_sha256 ~ '^[0-9a-f]{64}$'),
  observed_at     timestamptz not null default now()
);
create index if not exists metric_observations_tenant_key_sha
  on public.metric_observations (tenant_id, key, commit_sha);

create or replace function public.flow_reject_mutation() returns trigger
language plpgsql as $$
begin
  raise exception '% is append-only', tg_table_name;
end $$;

drop trigger if exists metric_observations_append_only on public.metric_observations;
create trigger metric_observations_append_only
  before update or delete on public.metric_observations
  for each row execute function public.flow_reject_mutation();

create table if not exists public.loop_rounds (
  id              bigint generated always as identity primary key,
  tenant_id       uuid not null references public.tenants(id),
  pr_number       integer not null,
  round           integer not null check (round >= 0),
  head_sha        text not null check (head_sha ~ '^[0-9a-f]{40}$'),
  role            text not null check (role in ('developer','evaluator','arbitrator','conductor')),
  envelope_digest text,
  tokens          bigint not null default 0,
  cost_usd        double precision not null default 0,
  created_at      timestamptz not null default now()
);
create index if not exists loop_rounds_tenant_pr on public.loop_rounds (tenant_id, pr_number);

-- Tenant scoping for pre-existing tables.
alter table public.repository_wikis
  add column if not exists tenant_id uuid references public.tenants(id) on delete cascade;
create unique index if not exists repository_wikis_tenant_uidx
  on public.repository_wikis (tenant_id) where tenant_id is not null;

alter table public.tenant_tokens
  add column if not exists tenant_id uuid references public.tenants(id) on delete cascade,
  add column if not exists label text,
  add column if not exists last_used_at timestamptz;
create index if not exists tenant_tokens_tenant on public.tenant_tokens (tenant_id);

alter table public.sessions
  add column if not exists tenant_id uuid references public.tenants(id),
  add column if not exists role text;

do $$
declare t text;
begin
  foreach t in array array['sessions','session_events','repository_wikis','tenant_tokens',
                           'artifacts','tenants','metric_definitions','metric_observations',
                           'loop_rounds']
  loop
    execute format('alter table public.%I enable row level security', t);
  end loop;
end $$;
