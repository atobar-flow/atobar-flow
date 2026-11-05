-- This Supabase project does not auto-grant new tables to API roles, so the
-- Control Panel's service_role got "permission denied" (42501). Grant explicitly.
-- anon/authenticated stay without access; RLS is enabled on every table.
grant usage on schema public to service_role;

grant select, insert, update, delete on
  public.tenants,
  public.repository_wikis,
  public.tenant_tokens,
  public.metric_definitions,
  public.loop_rounds,
  public.sessions,
  public.session_events,
  public.artifacts
to service_role;

-- Observations are append-only.
grant select, insert on public.metric_observations to service_role;

grant usage, select on all sequences in schema public to service_role;
