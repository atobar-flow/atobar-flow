-- 0016 created tenant_role_llm without API grants; this project does not auto-grant (see 0003),
-- so the Control Panel's service_role got 42501 on every read/write.
grant select, insert, update, delete on public.tenant_role_llm to service_role;
