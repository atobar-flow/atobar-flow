-- 0020: J34 service cutover, CP-authored metrics (J32), loop/Spot guardrails (#258),
-- and canonical manifest_sha provenance (#263).

alter table public.tenants
  add column if not exists spot_max_instance_minutes integer not null default 60
    check (spot_max_instance_minutes between 10 and 480);

alter table public.metric_definitions
  alter column source drop not null,
  alter column comparator drop not null,
  alter column threshold drop not null,
  add column if not exists intent text,
  add column if not exists kind text not null default 'guardrail'
    check (kind in ('guardrail', 'target')),
  add column if not exists owner_login text,
  add column if not exists rationale text,
  add column if not exists status text not null default 'active'
    check (status in ('draft', 'compiled', 'active', 'retired')),
  add column if not exists compiled jsonb,
  add column if not exists digest text,
  add column if not exists dry_run jsonb,
  add column if not exists approved_by text,
  add column if not exists approved_at timestamptz,
  drop constraint if exists metric_definitions_active_complete_chk,
  add constraint metric_definitions_active_complete_chk
    check (status = 'draft' or (source is not null and comparator is not null and threshold is not null));

alter table public.metric_observations
  add column if not exists manifest_sha text;

alter table public.arbitration_decisions
  add column if not exists manifest_sha text;

create table if not exists public.metric_definitions_history (
  id bigserial primary key,
  tenant_id uuid not null references public.tenants(id),
  key text not null,
  operation text not null,
  snapshot jsonb not null,
  changed_at timestamptz not null default now()
);

alter table public.metric_definitions_history enable row level security;
grant select, insert on public.metric_definitions_history to service_role;
grant usage, select on sequence public.metric_definitions_history_id_seq to service_role;

create or replace function public.record_metric_definitions_history()
returns trigger
language plpgsql
security definer
as $$
begin
  insert into public.metric_definitions_history (tenant_id, key, operation, snapshot)
  values (old.tenant_id, old.key, tg_op, to_jsonb(old));
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end;
$$;

drop trigger if exists trg_metric_definitions_history on public.metric_definitions;
create trigger trg_metric_definitions_history
  before update or delete on public.metric_definitions
  for each row execute function public.record_metric_definitions_history();

create table if not exists public.tenants_manifest_history (
  id bigserial primary key,
  tenant_id uuid not null references public.tenants(id),
  manifest jsonb not null,
  manifest_sha text not null,
  changed_at timestamptz not null default now()
);

alter table public.tenants_manifest_history enable row level security;
grant select, insert on public.tenants_manifest_history to service_role;
grant usage, select on sequence public.tenants_manifest_history_id_seq to service_role;

create or replace function public.record_tenants_manifest_history()
returns trigger
language plpgsql
security definer
as $$
begin
  if old.manifest is distinct from new.manifest or old.manifest_sha is distinct from new.manifest_sha then
    insert into public.tenants_manifest_history (tenant_id, manifest, manifest_sha)
    values (new.id, coalesce(new.manifest, '{}'::jsonb), coalesce(new.manifest_sha, ''));
  end if;
  return new;
end;
$$;

drop trigger if exists trg_tenants_manifest_history on public.tenants;
create trigger trg_tenants_manifest_history
  after update of manifest, manifest_sha on public.tenants
  for each row execute function public.record_tenants_manifest_history();

create or replace function public.approve_metric(
  p_tenant uuid,
  p_key text,
  p_digest text,
  p_login text,
  p_manifest jsonb,
  p_manifest_sha text
)
returns void
language plpgsql
security definer
as $$
declare
  v_row public.metric_definitions%rowtype;
begin
  select * into v_row
  from public.metric_definitions
  where tenant_id = p_tenant and key = p_key
  for update;

  if not found then
    raise exception 'metric_not_found' using errcode = 'P0002';
  end if;

  if v_row.status <> 'compiled' or coalesce(v_row.digest, '') <> coalesce(p_digest, '') then
    raise exception 'digest_mismatch' using errcode = 'P0001';
  end if;

  if v_row.source is null or v_row.comparator is null or v_row.threshold is null then
    raise exception 'incomplete_metric_definition' using errcode = '23514';
  end if;

  update public.metric_definitions
  set status = 'active',
      approved_by = p_login,
      approved_at = now(),
      updated_at = now()
  where tenant_id = p_tenant and key = p_key;

  update public.tenants
  set manifest = p_manifest,
      manifest_sha = p_manifest_sha,
      updated_at = now()
  where id = p_tenant;
end;
$$;

revoke all on function public.approve_metric(uuid, text, text, text, jsonb, text) from public;
grant execute on function public.approve_metric(uuid, text, text, text, jsonb, text) to service_role;
