-- 0007 tenant repository_id for GitHub Actions OIDC auth (M07feat01)
alter table public.tenants add column if not exists repository_id text;
create index if not exists tenants_repository_id_idx on public.tenants (repository_id);
