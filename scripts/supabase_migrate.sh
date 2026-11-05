#!/usr/bin/env bash
# Apply infra/supabase/migrations/*.sql in order, once each, to the Supabase DB.
# Connection comes from libpq env (PGHOST, PGUSER, PGPASSWORD, ...).
# Usage: scripts/supabase_migrate.sh [--dry-run]
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)/infra/supabase/migrations"
DRY_RUN="${1:-}"
export PGSSLMODE="${PGSSLMODE:-require}" PGDATABASE="${PGDATABASE:-postgres}"
PSQL=(psql -X -v ON_ERROR_STOP=1 -q -At)

"${PSQL[@]}" -c "create table if not exists public.flow_schema_migrations (
  version text primary key, sha256 text not null, applied_at timestamptz not null default now());
  alter table public.flow_schema_migrations enable row level security;"

for f in "$DIR"/[0-9][0-9][0-9][0-9]_*.sql; do
  v="$(basename "$f" .sql)"
  sum="$(sha256sum "$f" | cut -d' ' -f1)"
  applied="$("${PSQL[@]}" -c "select sha256 from public.flow_schema_migrations where version = '$v'")"
  if [ -n "$applied" ]; then
    if [ "$applied" != "$sum" ]; then
      echo "ERROR: $v was modified after being applied; add a new migration instead." >&2
      exit 1
    fi
    echo "skip  $v"
    continue
  fi
  if [ "$DRY_RUN" = "--dry-run" ]; then echo "would apply $v"; continue; fi
  echo "apply $v"
  "${PSQL[@]}" --single-transaction -f "$f" \
    -c "insert into public.flow_schema_migrations (version, sha256) values ('$v', '$sum')"
done
