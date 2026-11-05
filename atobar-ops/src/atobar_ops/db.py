"""Supabase access: REST client for app tables, direct `psql` for ad-hoc read-only SQL."""

from __future__ import annotations

import datetime
import difflib
import re
from typing import Any

from . import creds, timebox

SQL_ALLOWED_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SqlRejected(ValueError):
    pass


REST_TIMEOUT_SECONDS = 15


def client() -> Any:
    from supabase import ClientOptions, create_client

    c = creds.supabase_creds()
    # supabase-py's own default (120s) is far looser than any timeout this CLI
    # promises its callers — a single REST call must not be able to blow the
    # overall --timeout budget on its own.
    options = ClientOptions(postgrest_client_timeout=REST_TIMEOUT_SECONDS)
    return create_client(c.url, c.service_key, options=options)


def tenant_row(repo: str) -> dict[str, Any] | None:
    rows = (
        client()
        .table("tenants")
        .select("id,repo,status,arbitration_mode,default_branch,manifest_sha,updated_at")
        .eq("repo", repo)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def active_metric_count(tenant_id: str) -> int:
    rows = (
        client()
        .table("metric_definitions")
        .select("key,status")
        .eq("tenant_id", tenant_id)
        .execute()
        .data
        or []
    )
    return sum(1 for r in rows if (r.get("status") or "active") == "active")


def role_models(tenant_id: str) -> list[dict[str, Any]]:
    return (
        client()
        .table("tenant_role_llm")
        .select("role,provider,model,reasoning_effort,updated_at")
        .eq("tenant_id", tenant_id)
        .order("role")
        .execute()
        .data
        or []
    )


def llm_keys_rows(tenant_id: str) -> list[dict[str, Any]]:
    return (
        client()
        .table("tenant_llm_keys")
        .select("provider,key_last4,kek_version,ciphertext,validated_at,updated_at")
        .eq("tenant_id", tenant_id)
        .order("provider")
        .execute()
        .data
        or []
    )


def latest_run_id(tenant_id: str) -> str | None:
    rows = (
        client()
        .table("run_credentials")
        .select("run_id,role,created_at")
        .eq("tenant_id", tenant_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
        .data
    )
    return rows[0]["run_id"] if rows else None


def session_roles_by_instance(instance_ids: list[str]) -> dict[str, str]:
    """Role of the most recent session heartbeat for each instance id."""
    if not instance_ids:
        return {}
    rows = (
        client()
        .table("sessions")
        .select("instance_id,role,heartbeat_at")
        .in_("instance_id", instance_ids)
        .order("heartbeat_at", desc=True)
        .limit(50)
        .execute()
        .data
        or []
    )
    roles: dict[str, str] = {}
    for row in rows:
        iid, role = row.get("instance_id"), row.get("role")
        if iid and role and iid not in roles:
            roles[str(iid)] = str(role)
    return roles


def run_events(run_id: str, limit: int = 30) -> list[dict[str, Any]]:
    return (
        client()
        .table("run_events")
        .select("seq,ts,kind,payload")
        .eq("run_id", run_id)
        .order("seq", desc=False)
        .limit(limit)
        .execute()
        .data
        or []
    )


def loop_rounds(tenant_id: str | None = None, pr_number: int | None = None, limit: int = 15) -> list[dict[str, Any]]:
    q = client().table("loop_rounds").select(
        "tenant_id,pr_number,round,role,tokens,cost_usd,wall_clock_minutes,envelope_digest,usage_detail,created_at"
    )
    if tenant_id:
        q = q.eq("tenant_id", tenant_id)
    if pr_number:
        q = q.eq("pr_number", pr_number)
    return q.order("created_at", desc=True).limit(limit).execute().data or []


def arbitration_decisions(
    tenant_id: str | None = None, pr_number: int | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    q = client().table("arbitration_decisions").select(
        "tenant_id,pr_number,head_sha,mode,decision,reasons,manifest_sha,created_at"
    )
    if tenant_id:
        q = q.eq("tenant_id", tenant_id)
    if pr_number:
        q = q.eq("pr_number", pr_number)
    return q.order("created_at", desc=True).limit(limit).execute().data or []


def set_role_model(repo: str, role: str, provider: str, model: str, effort: str) -> dict[str, Any]:
    db = client()
    rows = db.table("tenants").select("id,repo").eq("repo", repo).limit(1).execute().data
    if not rows:
        raise LookupError(f"Tenant not found: {repo}")
    tid = rows[0]["id"]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    db.table("tenant_role_llm").upsert(
        {
            "tenant_id": tid,
            "role": role,
            "provider": provider,
            "model": model,
            "reasoning_effort": effort,
            "updated_at": now,
        },
        on_conflict="tenant_id,role",
    ).execute()
    return {"tenant_id": tid, "role": role, "provider": provider, "model": model, "effort": effort}


def validate_select(sql: str) -> str:
    """Raises SqlRejected unless `sql` is a single SELECT/WITH statement."""
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        raise SqlRejected("Only a single statement is allowed (no embedded ';')")
    if not SQL_ALLOWED_RE.match(stripped):
        raise SqlRejected("Only SELECT/WITH statements are allowed; use a named --write command for mutations")
    return stripped


def run_sql(sql: str, *, timeout: float = 30) -> tuple[int, str, str]:
    """Runs a validated read-only statement via psql against the pooler."""
    stmt = validate_select(sql)
    c = creds.supabase_creds()
    dsn = creds.pooler_dsn(c)
    res = timebox.run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-c", stmt], timeout=timeout)
    return res.returncode, res.stdout, res.stderr


def _query_single_column(sql: str, *, timeout: float = 10) -> list[str]:
    """Executes a read-only single-column query and parses non-header rows."""
    try:
        rc, out, _ = run_sql(sql, timeout=timeout)
    except Exception:
        return []
    if rc != 0 or not out:
        return []
    values: list[str] = []
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith("(") and ln.endswith(")") and "row" in ln:
            continue
        if set(ln) <= {"-", "+", "|", " "}:
            continue
        if ln in ("table_name", "column_name"):
            continue
        values.append(ln.split("|", 1)[0].strip())
    return values


def list_public_tables(*, timeout: float = 10) -> list[str]:
    return _query_single_column(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name",
        timeout=timeout,
    )


def list_table_columns(table: str, *, timeout: float = 10) -> list[str]:
    if not _IDENT_RE.match(table):
        return []
    return _query_single_column(
        f"SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema = 'public' AND table_name = '{table}' ORDER BY ordinal_position",
        timeout=timeout,
    )


def schema_tables(*, timeout: float = 30) -> tuple[int, str, str]:
    """Lists public tables with row-count estimates from pg_class."""
    sql = (
        "SELECT c.relname AS table_name, GREATEST(c.reltuples::bigint, 0) AS est_rows "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY c.relname"
    )
    return run_sql(sql, timeout=timeout)


def schema_columns(table: str, *, timeout: float = 30) -> tuple[int, str, str]:
    """Lists column | data_type | is_nullable for one public table."""
    clean = table.strip()
    if clean.lower().startswith("public."):
        clean = clean.split(".", 1)[1]
    if not _IDENT_RE.match(clean):
        raise SqlRejected(f"Invalid table name: {table!r}")
    sql = (
        f"SELECT column_name, data_type, is_nullable "
        f"FROM information_schema.columns "
        f"WHERE table_schema = 'public' AND table_name = '{clean}' "
        f"ORDER BY ordinal_position"
    )
    rc, out, err = run_sql(sql, timeout=timeout)
    if rc == 0 and "(0 rows)" in out:
        tables = list_public_tables(timeout=min(10.0, timeout))
        matches = difflib.get_close_matches(clean, tables, n=3, cutoff=0.35)
        if matches:
            hint = f"Did you mean: {', '.join(matches)}? (public tables: {', '.join(tables)})"
        elif tables:
            hint = f"Did you mean one of public tables: {', '.join(tables)}"
        else:
            hint = f"Table {clean!r} not found in schema public"
        return 1, out, f"{err}\n{hint}".strip()
    return rc, out, err


_MISSING_REL_RE = re.compile(r'relation "(?:public\.)?([A-Za-z0-9_]+)" does not exist', re.IGNORECASE)
_MISSING_COL_RE = re.compile(
    r'column (?:"?([A-Za-z0-9_]+)"?\.)?"?([A-Za-z0-9_]+)"?(?: of relation "([A-Za-z0-9_]+)")? does not exist',
    re.IGNORECASE,
)
_SQL_TABLES_RE = re.compile(r"\b(?:from|join)\s+(?:public\.)?([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def format_sql_error_hint(sql: str, stderr: str, *, timeout: float = 10) -> str:
    """Builds a `Did you mean:` hint on undefined_table (42P01) or undefined_column (42703)."""
    m_rel = _MISSING_REL_RE.search(stderr)
    if m_rel or "42P01" in stderr:
        bad_table = m_rel.group(1) if m_rel else ""
        if not bad_table:
            tbls_in_sql = _SQL_TABLES_RE.findall(sql)
            bad_table = tbls_in_sql[0] if tbls_in_sql else ""
        tables = list_public_tables(timeout=timeout)
        if not tables:
            return ""
        matches = difflib.get_close_matches(bad_table, tables, n=3, cutoff=0.35) if bad_table else []
        if matches:
            return f"Did you mean: {', '.join(matches)}? (public tables: {', '.join(tables)})"
        return f"Did you mean: {', '.join(tables[:5])}? (public tables: {', '.join(tables)})"

    m_col = _MISSING_COL_RE.search(stderr)
    if m_col or "42703" in stderr:
        qual, bad_col, rel_from_err = (m_col.group(1), m_col.group(2), m_col.group(3)) if m_col else ("", "", "")
        candidate_tables: list[str] = []
        for t in [rel_from_err, *_SQL_TABLES_RE.findall(sql), qual]:
            if t and _IDENT_RE.match(t) and t not in candidate_tables:
                candidate_tables.append(t)
        for tbl in candidate_tables:
            cols = list_table_columns(tbl, timeout=timeout)
            if not cols:
                continue
            matches = difflib.get_close_matches(bad_col, cols, n=3, cutoff=0.35) if bad_col else []
            if matches:
                return f"Did you mean: {', '.join(matches)}? (columns on {tbl}: {', '.join(cols)})"
            return f"Did you mean: {', '.join(cols)} (columns on {tbl})"
    return ""


def run_migration_file(path: str, *, timeout: float = 30) -> tuple[int, str, str]:
    """Applies an idempotent SQL migration file under infra/supabase/migrations/ via psql."""
    c = creds.supabase_creds()
    dsn = creds.pooler_dsn(c)
    res = timebox.run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-f", path], timeout=timeout)
    return res.returncode, res.stdout, res.stderr


def prune_plan(tenant_id: str, pr_numbers: list[int]) -> dict[str, int]:
    """Counts rows scoped to `tenant_id` + `pr_numbers` in the tables `rerun --write` may prune."""
    db = client()
    counts: dict[str, int] = {}
    for table in ("loop_rounds", "arbitration_decisions"):
        rows = (
            db.table(table)
            .select("pr_number")
            .eq("tenant_id", tenant_id)
            .in_("pr_number", pr_numbers)
            .execute()
            .data
            or []
        )
        counts[table] = len(rows)
    return counts


def prune_execute(tenant_id: str, pr_numbers: list[int]) -> dict[str, int]:
    """Deletes rows scoped to `tenant_id` + `pr_numbers` from `loop_rounds` and `arbitration_decisions` ONLY."""
    db = client()
    counts = prune_plan(tenant_id, pr_numbers)
    for table in ("loop_rounds", "arbitration_decisions"):
        if counts.get(table):
            db.table(table).delete().eq("tenant_id", tenant_id).in_("pr_number", pr_numbers).execute()
    return counts
