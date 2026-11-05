-- Migration 0010: Add usage_detail jsonb column to loop_rounds
alter table if exists public.loop_rounds
  add column if not exists usage_detail jsonb;

grant select, insert, update on public.loop_rounds to service_role;
