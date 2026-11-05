-- 0021: Allow planner role in loop_rounds and extended swarm roles in tenant_role_llm (#167).

alter table public.loop_rounds
  drop constraint if exists loop_rounds_role_check,
  add constraint loop_rounds_role_check
    check (role in ('planner', 'developer', 'evaluator', 'arbitrator', 'conductor'));

alter table public.tenant_role_llm
  drop constraint if exists tenant_role_llm_role_check,
  add constraint tenant_role_llm_role_check
    check (role in ('planner', 'developer', 'evaluator', 'arbitrator', 'onboarder', 'metric_compiler'));
