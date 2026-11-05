-- 0001 baseline: Central Control Plane / Session Registry (Axiom 10).
-- Applied by scripts/supabase_migrate.sh (see .github/workflows/supabase-migrate.yml).
-- Git remains the durable recovery state (Axiom 6).

create table if not exists public.sessions (
    instance_id   text primary key,
    target_repo   text        not null,
    issue_id      bigint,
    status        text        not null default 'Running',
    started_at    timestamptz not null default now(),
    heartbeat_at  timestamptz not null default now(),
    current_todo  text,
    last_tool     text,
    cost_estimate double precision not null default 0
);

-- Extend existing sessions table with lifecycle & trace pointers
alter table public.sessions
  add column if not exists issue_url text,
  add column if not exists pr_url text,
  add column if not exists trace_head_seq bigint default 0,
  add column if not exists halted_reason text;

-- Fleet-view helpers: filter by liveness/status and sort by freshness.
create index if not exists sessions_status_idx    on public.sessions (status);
create index if not exists sessions_heartbeat_idx on public.sessions (heartbeat_at desc);

-- #111 unified event trace (drives the Control Panel replay view)
create table if not exists public.session_events (
  id          bigint generated always as identity primary key,
  instance_id text not null references public.sessions(instance_id),
  seq         bigint not null,
  type        text not null check (type in
              ('thought','tool_call','tool_result','pty_stream','state','lifecycle')),
  payload     jsonb not null,
  created_at  timestamptz not null default now(),
  unique (instance_id, seq)
);
create index if not exists session_events_inst_seq on public.session_events (instance_id, seq);

-- #115 central wiki storage (decoupled from client repos)
create table if not exists public.repository_wikis (
  repo_name    text primary key,
  purpose      text,
  technologies jsonb,
  structure    text,
  concepts     jsonb,
  last_updated timestamptz not null default now()
);

-- #100 Phase 2 zero-config onboarding (store hashes, never plaintext)
create table if not exists public.tenant_tokens (
  token_hash         text primary key,
  owner_github_login text not null,
  created_at         timestamptz not null default now(),
  revoked_at         timestamptz
);

-- #113 visual evidence pointers (objects live in Supabase Storage / S3)
create table if not exists public.artifacts (
  id           bigint generated always as identity primary key,
  instance_id  text references public.sessions(instance_id),
  kind         text not null check (kind in ('screenshot','overflow_log','other')),
  storage_path text not null,
  created_at   timestamptz not null default now()
);
