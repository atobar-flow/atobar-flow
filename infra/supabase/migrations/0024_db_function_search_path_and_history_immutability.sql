-- 0024: Pin search_path on security-definer/trigger functions and enforce immutability on audit history tables (J43 WP-5)

ALTER FUNCTION public.record_metric_definitions_history() SET search_path = public;
ALTER FUNCTION public.record_tenants_manifest_history() SET search_path = public;
ALTER FUNCTION public.approve_metric(uuid, text, text, text, jsonb, text) SET search_path = public;
ALTER FUNCTION public.flow_reject_mutation() SET search_path = public;

DROP TRIGGER IF EXISTS metric_definitions_history_immutable ON public.metric_definitions_history;
CREATE TRIGGER metric_definitions_history_immutable
  BEFORE UPDATE OR DELETE ON public.metric_definitions_history
  FOR EACH ROW EXECUTE FUNCTION public.flow_reject_mutation();

DROP TRIGGER IF EXISTS tenants_manifest_history_immutable ON public.tenants_manifest_history;
CREATE TRIGGER tenants_manifest_history_immutable
  BEFORE UPDATE OR DELETE ON public.tenants_manifest_history
  FOR EACH ROW EXECUTE FUNCTION public.flow_reject_mutation();

-- run_events blocks UPDATE only so ON DELETE CASCADE from run_credentials continues to work during tenant offboarding
DROP TRIGGER IF EXISTS run_events_no_update ON public.run_events;
CREATE TRIGGER run_events_no_update
  BEFORE UPDATE ON public.run_events
  FOR EACH ROW EXECUTE FUNCTION public.flow_reject_mutation();
