-- 0023: J42 WP-1 envelope authenticity — bind run_credentials to dispatched head_sha and single envelope_comment_id (#337).

alter table public.run_credentials
  add column if not exists head_sha text,
  add column if not exists envelope_comment_id bigint;
