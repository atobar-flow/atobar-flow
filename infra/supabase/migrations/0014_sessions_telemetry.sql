-- 0014: sessions telemetry columns and conductor_events audit payload
alter table public.sessions
  add column if not exists usage jsonb,
  add column if not exists iterations integer not null default 0,
  add column if not exists agent_runtime text,
  add column if not exists last_activity_at timestamptz,
  add column if not exists head_sha text,
  add column if not exists halt_reason text,
  add column if not exists halt_detail text;

alter table public.conductor_events
  add column if not exists payload jsonb,
  add column if not exists kind text,
  add column if not exists sender text;
