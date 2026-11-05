-- 0015: append-only run_events audit stream
create table if not exists public.run_events (
  id       bigint generated always as identity primary key,
  run_id   text references public.run_credentials(run_id) on delete cascade,
  seq      bigint not null,
  kind     text not null,
  ts       timestamptz not null default now(),
  payload  jsonb not null default '{}'::jsonb,
  unique (run_id, seq)
);

create index if not exists run_events_run_id_idx on public.run_events (run_id);
create index if not exists run_events_kind_idx on public.run_events (kind);

alter table public.run_events enable row level security;

create policy "service_role_all" on public.run_events for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

grant select, insert on public.run_events to service_role;
