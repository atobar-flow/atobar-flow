-- 0026: SotaWiki lifecycle columns, atomic claim/upsert/completion/policy RPCs, and coalesced refresh queue (#368).

alter table public.repository_wikis
  add column if not exists architecture_mermaid text not null default '',
  add column if not exists commit_sha text not null default '',
  add column if not exists refresh_status text not null default 'idle' check (refresh_status in ('idle', 'queued', 'running', 'failed')),
  add column if not exists refresh_queued boolean not null default false,
  add column if not exists refresh_run_id text,
  add column if not exists refresh_started_at timestamptz,
  add column if not exists last_error text,
  add column if not exists last_observed_sha text not null default '',
  add column if not exists merges_since_refresh integer not null default 0,
  add column if not exists refresh_interval_merges integer not null default 5 check (refresh_interval_merges >= 0 and refresh_interval_merges <= 100);

create or replace function public.flow_record_default_branch_merge_for_wiki(
  p_tenant_id uuid,
  p_commit_sha text
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_repo text;
  v_row public.repository_wikis%rowtype;
begin
  select repo into v_repo from public.tenants where id = p_tenant_id;
  if v_repo is null then
    return '{}'::jsonb;
  end if;

  update public.repository_wikis
     set repo_name = v_repo
   where tenant_id = p_tenant_id
     and repo_name <> v_repo
     and not exists (select 1 from public.repository_wikis where repo_name = v_repo);

  update public.repository_wikis
     set tenant_id = p_tenant_id
   where repo_name = v_repo
     and tenant_id is null
     and not exists (select 1 from public.repository_wikis where tenant_id = p_tenant_id);

  insert into public.repository_wikis (
    tenant_id,
    repo_name,
    purpose,
    technologies,
    structure,
    concepts,
    last_observed_sha,
    merges_since_refresh
  ) values (
    p_tenant_id,
    v_repo,
    '',
    '[]'::jsonb,
    '',
    '[]'::jsonb,
    coalesce(p_commit_sha, ''),
    case when coalesce(p_commit_sha, '') <> '' then 1 else 0 end
  )
  on conflict (repo_name) do update
     set tenant_id = coalesce(public.repository_wikis.tenant_id, excluded.tenant_id),
         merges_since_refresh = case
           when coalesce(p_commit_sha, '') <> ''
            and p_commit_sha is distinct from nullif(public.repository_wikis.last_observed_sha, '')
            and p_commit_sha is distinct from nullif(public.repository_wikis.commit_sha, '')
           then public.repository_wikis.merges_since_refresh + 1
           else public.repository_wikis.merges_since_refresh
         end,
         last_observed_sha = case
           when coalesce(p_commit_sha, '') <> ''
           then p_commit_sha
           else public.repository_wikis.last_observed_sha
         end
  returning * into v_row;

  return to_jsonb(v_row);
end;
$$;
revoke all on function public.flow_record_default_branch_merge_for_wiki(uuid, text) from public, anon, authenticated;
grant execute on function public.flow_record_default_branch_merge_for_wiki(uuid, text) to service_role;

create or replace function public.flow_upsert_wiki_content(
  p_tenant_id uuid,
  p_repo_name text,
  p_purpose text,
  p_technologies jsonb,
  p_structure text,
  p_concepts jsonb,
  p_architecture_mermaid text,
  p_commit_sha text
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.repository_wikis%rowtype;
begin
  update public.repository_wikis
     set repo_name = p_repo_name
   where tenant_id = p_tenant_id
     and repo_name <> p_repo_name
     and not exists (select 1 from public.repository_wikis where repo_name = p_repo_name);

  update public.repository_wikis
     set tenant_id = p_tenant_id
   where repo_name = p_repo_name
     and tenant_id is null
     and not exists (select 1 from public.repository_wikis where tenant_id = p_tenant_id);

  insert into public.repository_wikis (
    tenant_id,
    repo_name,
    purpose,
    technologies,
    structure,
    concepts,
    architecture_mermaid,
    commit_sha,
    last_observed_sha,
    refresh_status,
    refresh_queued,
    last_error,
    merges_since_refresh,
    last_updated
  ) values (
    p_tenant_id,
    p_repo_name,
    coalesce(p_purpose, ''),
    coalesce(p_technologies, '[]'::jsonb),
    coalesce(p_structure, ''),
    coalesce(p_concepts, '[]'::jsonb),
    coalesce(p_architecture_mermaid, ''),
    coalesce(p_commit_sha, ''),
    coalesce(p_commit_sha, ''),
    'idle',
    false,
    null,
    0,
    now()
  )
  on conflict (repo_name) do update
     set tenant_id = coalesce(public.repository_wikis.tenant_id, excluded.tenant_id),
         purpose = excluded.purpose,
         technologies = excluded.technologies,
         structure = excluded.structure,
         concepts = excluded.concepts,
         architecture_mermaid = case
           when excluded.architecture_mermaid <> '' then excluded.architecture_mermaid
           else public.repository_wikis.architecture_mermaid
         end,
         commit_sha = case
           when excluded.commit_sha <> '' then excluded.commit_sha
           else public.repository_wikis.commit_sha
         end,
         last_observed_sha = case
           when coalesce(public.repository_wikis.last_observed_sha, '') = ''
             or public.repository_wikis.last_observed_sha = excluded.commit_sha
             or excluded.commit_sha = ''
           then coalesce(nullif(excluded.commit_sha, ''), public.repository_wikis.last_observed_sha)
           else public.repository_wikis.last_observed_sha
         end,
         merges_since_refresh = case
           when coalesce(public.repository_wikis.last_observed_sha, '') = ''
             or public.repository_wikis.last_observed_sha = excluded.commit_sha
             or excluded.commit_sha = ''
           then 0
           else public.repository_wikis.merges_since_refresh
         end,
         refresh_status = case
           when public.repository_wikis.refresh_queued then 'queued'
           when public.repository_wikis.refresh_run_id is not null and public.repository_wikis.refresh_run_id <> '' then public.repository_wikis.refresh_status
           else 'idle'
         end,
         last_error = null,
         last_updated = now()
  returning * into v_row;

  return to_jsonb(v_row);
end;
$$;
revoke all on function public.flow_upsert_wiki_content(uuid, text, text, jsonb, text, jsonb, text, text) from public, anon, authenticated;
grant execute on function public.flow_upsert_wiki_content(uuid, text, text, jsonb, text, jsonb, text, text) to service_role;

create or replace function public.flow_update_wiki_policy(
  p_tenant_id uuid,
  p_repo_name text,
  p_refresh_interval_merges integer
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.repository_wikis%rowtype;
  v_interval integer := greatest(0, least(100, coalesce(p_refresh_interval_merges, 5)));
begin
  update public.repository_wikis
     set repo_name = p_repo_name
   where tenant_id = p_tenant_id
     and repo_name <> p_repo_name
     and not exists (select 1 from public.repository_wikis where repo_name = p_repo_name);

  update public.repository_wikis
     set tenant_id = p_tenant_id
   where repo_name = p_repo_name
     and tenant_id is null
     and not exists (select 1 from public.repository_wikis where tenant_id = p_tenant_id);

  insert into public.repository_wikis (
    tenant_id,
    repo_name,
    purpose,
    technologies,
    structure,
    concepts,
    refresh_interval_merges
  ) values (
    p_tenant_id,
    p_repo_name,
    '',
    '[]'::jsonb,
    '',
    '[]'::jsonb,
    v_interval
  )
  on conflict (repo_name) do update
     set tenant_id = coalesce(public.repository_wikis.tenant_id, excluded.tenant_id),
         refresh_interval_merges = excluded.refresh_interval_merges
  returning * into v_row;

  return to_jsonb(v_row);
end;
$$;
revoke all on function public.flow_update_wiki_policy(uuid, text, integer) from public, anon, authenticated;
grant execute on function public.flow_update_wiki_policy(uuid, text, integer) to service_role;

create or replace function public.flow_claim_wiki_refresh(
  p_tenant_id uuid,
  p_repo_name text,
  p_run_id text,
  p_ttl_minutes integer default 15
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.repository_wikis%rowtype;
  v_live_session_run text;
  v_active_run text;
begin
  update public.repository_wikis
     set repo_name = p_repo_name
   where tenant_id = p_tenant_id
     and repo_name <> p_repo_name
     and not exists (select 1 from public.repository_wikis where repo_name = p_repo_name);

  update public.repository_wikis
     set tenant_id = p_tenant_id
   where repo_name = p_repo_name
     and tenant_id is null
     and not exists (select 1 from public.repository_wikis where tenant_id = p_tenant_id);

  insert into public.repository_wikis (tenant_id, repo_name, purpose, technologies, structure, concepts)
  values (p_tenant_id, p_repo_name, '', '[]'::jsonb, '', '[]'::jsonb)
  on conflict (repo_name) do update
     set tenant_id = coalesce(public.repository_wikis.tenant_id, excluded.tenant_id);

  select * into v_row
    from public.repository_wikis
   where repo_name = p_repo_name
   for update;

  select run_id into v_live_session_run
    from public.sessions
   where tenant_id = p_tenant_id
     and role = 'onboarder'
     and lower(coalesce(status, '')) not similar to '%(complete|completed|halted|failed|terminated|stopped|dispatched)%'
     and coalesce(heartbeat_at, started_at) >= now() - interval '10 minutes'
   order by heartbeat_at desc nulls last
   limit 1;

  if v_row.refresh_run_id is not null
     and v_row.refresh_run_id <> ''
     and v_row.refresh_run_id <> p_run_id
     and v_row.refresh_started_at is not null
     and v_row.refresh_started_at >= now() - make_interval(mins => greatest(1, coalesce(p_ttl_minutes, 15))) then
    v_active_run := v_row.refresh_run_id;
  elsif v_live_session_run is not null and v_live_session_run <> '' and v_live_session_run <> p_run_id then
    v_active_run := v_live_session_run;
  else
    v_active_run := null;
  end if;

  if v_active_run is not null then
    update public.repository_wikis
       set refresh_queued = true,
           refresh_status = 'queued'
     where repo_name = p_repo_name;
    return jsonb_build_object(
      'claimed', false,
      'queued', true,
      'active_run_id', v_active_run
    );
  end if;

  update public.repository_wikis
     set refresh_run_id = p_run_id,
         refresh_started_at = now(),
         refresh_status = 'running',
         refresh_queued = false,
         last_error = null
   where repo_name = p_repo_name;

  return jsonb_build_object(
    'claimed', true,
    'queued', false,
    'active_run_id', p_run_id
  );
end;
$$;
revoke all on function public.flow_claim_wiki_refresh(uuid, text, text, integer) from public, anon, authenticated;
grant execute on function public.flow_claim_wiki_refresh(uuid, text, text, integer) to service_role;

create or replace function public.flow_complete_wiki_refresh_run(
  p_tenant_id uuid,
  p_repo_name text,
  p_run_id text,
  p_error text default null
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.repository_wikis%rowtype;
  v_was_queued boolean;
  v_other_live boolean;
begin
  select * into v_row
    from public.repository_wikis
   where tenant_id = p_tenant_id or repo_name = p_repo_name
   order by (tenant_id = p_tenant_id) desc nulls last
   limit 1
   for update;

  if not found or coalesce(p_run_id, '') = '' then
    return jsonb_build_object('finalized', false, 'drain_queued', false);
  end if;

  if coalesce(v_row.refresh_run_id, '') <> p_run_id then
    select exists(
      select 1
        from public.sessions
       where tenant_id = p_tenant_id
         and role = 'onboarder'
         and coalesce(run_id, '') <> p_run_id
         and lower(coalesce(status, '')) not similar to '%(complete|completed|halted|failed|terminated|stopped|dispatched)%'
         and coalesce(heartbeat_at, started_at) >= now() - interval '10 minutes'
    ) into v_other_live;

    if coalesce(v_row.refresh_run_id, '') <> '' or not coalesce(v_row.refresh_queued, false) or v_other_live then
      return jsonb_build_object('finalized', false, 'drain_queued', false);
    end if;
  end if;

  v_was_queued := coalesce(v_row.refresh_queued, false);

  update public.repository_wikis
     set refresh_run_id = null,
         refresh_started_at = null,
         refresh_queued = false,
         merges_since_refresh = case
           when p_error is not null and p_error <> '' then 0
           else public.repository_wikis.merges_since_refresh
         end,
         refresh_status = case
           when v_was_queued then 'queued'
           when p_error is not null and p_error <> '' then 'failed'
           else 'idle'
         end,
         last_error = case
           when p_error is not null and p_error <> '' then p_error
           else null
         end
   where repo_name = v_row.repo_name;

  return jsonb_build_object('finalized', true, 'drain_queued', v_was_queued);
end;
$$;
revoke all on function public.flow_complete_wiki_refresh_run(uuid, text, text, text) from public, anon, authenticated;
grant execute on function public.flow_complete_wiki_refresh_run(uuid, text, text, text) to service_role;
