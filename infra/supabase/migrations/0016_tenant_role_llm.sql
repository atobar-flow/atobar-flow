-- 0016: per-role model selection per tenant (repo), set in the Control Panel UI.
-- The only source of a role's provider/model/reasoning effort; runs fail without a row.
create table public.tenant_role_llm (
  tenant_id uuid not null references public.tenants(id) on delete cascade,
  role text not null check (role in ('developer', 'evaluator', 'arbitrator')),
  provider text not null,
  model text not null,
  reasoning_effort text not null default 'none' check (reasoning_effort in ('none', 'low', 'medium', 'high')),
  updated_at timestamptz not null default now(),
  primary key (tenant_id, role)
);
alter table public.tenant_role_llm enable row level security;

-- Carry over the per-key selection made before roles existed.
insert into public.tenant_role_llm (tenant_id, role, provider, model, reasoning_effort)
select k.tenant_id, r.role, k.provider, k.selected_model,
       case k.thinking_level when 'high' then 'high' when 'standard' then 'medium' else 'none' end
from public.tenant_llm_keys k
cross join (values ('developer'), ('evaluator')) as r(role)
where coalesce(k.selected_model, '') <> ''
on conflict (tenant_id, role) do nothing;
