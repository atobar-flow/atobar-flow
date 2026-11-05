"""Real PostgreSQL execution test for 0026_wiki_lifecycle.sql PL/pgSQL RPCs (#368)."""

import json
import pathlib
import shutil
import subprocess
from typing import Any

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
MIGRATIONS_DIR = REPO_ROOT / "infra" / "supabase" / "migrations"


def _pg_bin(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for cand in sorted(pathlib.Path("/usr/lib/postgresql").glob(f"*/bin/{name}"), reverse=True):
        if cand.is_file():
            return str(cand)
    return None


@pytest.fixture(scope="module")
def pg_socket_dir(tmp_path_factory: pytest.TempPathFactory):
    import tempfile

    initdb_bin = _pg_bin("initdb")
    pg_ctl_bin = _pg_bin("pg_ctl")
    psql_bin = _pg_bin("psql")
    if not (initdb_bin and pg_ctl_bin and psql_bin):
        pytest.skip("PostgreSQL binaries (initdb, pg_ctl, psql) not available")
    base = tmp_path_factory.mktemp("pg_wiki")
    datadir = base / "data"
    sockdir = pathlib.Path(tempfile.mkdtemp(prefix="atb_pg_", dir="/tmp"))

    subprocess.run(
        [initdb_bin, "-D", str(datadir), "-U", "postgres", "--no-locale", "-E", "UTF8", "-A", "trust"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            pg_ctl_bin,
            "-D",
            str(datadir),
            "-l",
            str(base / "pg.log"),
            "-o",
            f"-k {sockdir} -h ''",
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        def _psql(sql: str) -> str:
            res = subprocess.run(
                [psql_bin, "-h", str(sockdir), "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-Atq"],
                input=sql,
                check=True,
                capture_output=True,
                text=True,
            )
            return res.stdout.strip()

        _psql(
            """
            create role anon nologin;
            create role authenticated nologin;
            create role service_role nologin;
            """
        )
        _psql((MIGRATIONS_DIR / "0001_baseline.sql").read_text(encoding="utf-8"))
        _psql((MIGRATIONS_DIR / "0002_tenancy.sql").read_text(encoding="utf-8"))
        _psql((MIGRATIONS_DIR / "0006_run_credentials.sql").read_text(encoding="utf-8"))
        _psql("alter table public.sessions add column if not exists run_id text;")
        _psql((MIGRATIONS_DIR / "0026_wiki_lifecycle.sql").read_text(encoding="utf-8"))
        yield _psql
    finally:
        subprocess.run([pg_ctl_bin, "-D", str(datadir), "-m", "immediate", "stop"], check=False, capture_output=True)
        shutil.rmtree(sockdir, ignore_errors=True)


def test_sql_wiki_rpcs_claim_coalesce_upsert_complete_policy_and_rename(pg_socket_dir: Any) -> None:
    psql = pg_socket_dir
    tid = psql(
        """
        insert into public.tenants (repo, status, arbitration_mode, manifest, manifest_sha)
        values ('arsenyspb/ollama-monitor', 'active', 'shadow', '{}'::jsonb, 'sha0')
        returning id;
        """
    )

    # 1. Verify PUBLIC / anon / authenticated cannot execute any of the 5 SECURITY DEFINER functions
    privs = psql(
        """
        select bool_or(has_function_privilege('anon', oid, 'EXECUTE'))
               or bool_or(has_function_privilege('authenticated', oid, 'EXECUTE'))
          from pg_proc
         where proname in (
           'flow_record_default_branch_merge_for_wiki',
           'flow_upsert_wiki_content',
           'flow_update_wiki_policy',
           'flow_claim_wiki_refresh',
           'flow_complete_wiki_refresh_run'
         );
        """
    )
    assert privs == "f"

    # 2. Legacy row with tenant_id IS NULL: flow_update_wiki_policy attaches tenant_id and preserves content!
    psql(
        """
        insert into public.repository_wikis (repo_name, purpose, technologies, structure, concepts, architecture_mermaid)
        values ('arsenyspb/ollama-monitor', 'Legacy purpose', '["Python"]'::jsonb, 'src/', '["TUI"]'::jsonb, 'flowchart TD\n  A-->B');
        """
    )
    pol_row = json.loads(
        psql(f"select public.flow_update_wiki_policy('{tid}'::uuid, 'arsenyspb/ollama-monitor', 3);")
    )
    assert pol_row["tenant_id"] == tid
    assert pol_row["refresh_interval_merges"] == 3
    assert pol_row["purpose"] == "Legacy purpose"
    assert pol_row["architecture_mermaid"] == "flowchart TD\n  A-->B"

    # 3. Claim refresh run-1 -> claimed=true; concurrent claim run-2 -> claimed=false, queued=true
    c1 = json.loads(
        psql(f"select public.flow_claim_wiki_refresh('{tid}'::uuid, 'arsenyspb/ollama-monitor', 'run-1', 15);")
    )
    assert c1 == {"claimed": True, "queued": False, "active_run_id": "run-1"}

    c2 = json.loads(
        psql(f"select public.flow_claim_wiki_refresh('{tid}'::uuid, 'arsenyspb/ollama-monitor', 'run-2', 15);")
    )
    assert c2 == {"claimed": False, "queued": True, "active_run_id": "run-1"}

    # 4. Intervening default-branch merge arrives at sha_b while run-1 synthesizes sha_a:
    #    flow_upsert_wiki_content preserves refresh_queued=true, refresh_interval_merges=3, and merges_since_refresh=1!
    m1 = json.loads(
        psql(f"select public.flow_record_default_branch_merge_for_wiki('{tid}'::uuid, '{'b' * 40}');")
    )
    assert m1["merges_since_refresh"] == 1

    up_row = json.loads(
        psql(
            f"""
            select public.flow_upsert_wiki_content(
              '{tid}'::uuid,
              'arsenyspb/ollama-monitor',
              'Updated purpose',
              '["Python", "FastAPI"]'::jsonb,
              'src/app.py',
              '["SSE"]'::jsonb,
              'flowchart LR\n  UI --> API',
              '{'a' * 40}'
            );
            """
        )
    )
    assert up_row["purpose"] == "Updated purpose"
    assert up_row["refresh_queued"] is True
    assert up_row["refresh_interval_merges"] == 3
    assert up_row["merges_since_refresh"] == 1, "Intervening merge (sha_b != sha_a) must not be erased by upsert"

    # 5. Complete run-1 -> finalized=true, drain_queued=true; retried completion of run-1 -> finalized=false
    fin1 = json.loads(
        psql(f"select public.flow_complete_wiki_refresh_run('{tid}'::uuid, 'arsenyspb/ollama-monitor', 'run-1', null);")
    )
    assert fin1 == {"finalized": True, "drain_queued": True}

    fin1_retry = json.loads(
        psql(f"select public.flow_complete_wiki_refresh_run('{tid}'::uuid, 'arsenyspb/ollama-monitor', 'run-1', null);")
    )
    assert fin1_retry == {"finalized": False, "drain_queued": False}

    # 6. Failed refresh resets merges_since_refresh = 0
    psql(f"select public.flow_claim_wiki_refresh('{tid}'::uuid, 'arsenyspb/ollama-monitor', 'run-fail', 15);")
    fin_fail = json.loads(
        psql(
            f"select public.flow_complete_wiki_refresh_run('{tid}'::uuid, 'arsenyspb/ollama-monitor', 'run-fail', 'boom');"
        )
    )
    assert fin_fail == {"finalized": True, "drain_queued": False}
    after_fail = json.loads(
        psql(f"select to_jsonb(r) from public.repository_wikis r where tenant_id = '{tid}'::uuid;")
    )
    assert after_fail["refresh_status"] == "failed"
    assert after_fail["merges_since_refresh"] == 0

    # 7. Repo rename: updating tenant repo_name reconciles the existing row without violating repository_wikis_tenant_uidx
    psql(f"update public.tenants set repo = 'arsenyspb/ollama-monitor-v2' where id = '{tid}'::uuid;")
    c_renamed = json.loads(
        psql(f"select public.flow_claim_wiki_refresh('{tid}'::uuid, 'arsenyspb/ollama-monitor-v2', 'run-renamed', 15);")
    )
    assert c_renamed["claimed"] is True
    count_rows = psql(f"select count(*) from public.repository_wikis where tenant_id = '{tid}'::uuid;")
    assert count_rows == "1"

    # 8. Unclaimed live onboarder session in public.sessions queues refresh, and completing that session drains the queue
    psql(
        f"select public.flow_complete_wiki_refresh_run('{tid}'::uuid, 'arsenyspb/ollama-monitor-v2', 'run-renamed', null);"
    )
    psql(
        f"""
        insert into public.sessions (instance_id, run_id, tenant_id, target_repo, role, status, heartbeat_at)
        values ('i-onb-live', 'run-unclaimed-onb', '{tid}'::uuid, 'arsenyspb/ollama-monitor-v2', 'onboarder', 'Running', now());
        """
    )
    c_queued_by_sess = json.loads(
        psql(
            f"select public.flow_claim_wiki_refresh('{tid}'::uuid, 'arsenyspb/ollama-monitor-v2', 'run-later', 15);"
        )
    )
    assert c_queued_by_sess == {"claimed": False, "queued": True, "active_run_id": "run-unclaimed-onb"}

    psql("update public.sessions set status = 'Complete' where instance_id = 'i-onb-live';")
    fin_unclaimed = json.loads(
        psql(
            f"select public.flow_complete_wiki_refresh_run('{tid}'::uuid, 'arsenyspb/ollama-monitor-v2', 'run-unclaimed-onb', null);"
        )
    )
    assert fin_unclaimed == {"finalized": True, "drain_queued": True}
