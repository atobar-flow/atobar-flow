import argparse

import pytest

from atobar_ops import cli, db


@pytest.mark.parametrize(
    "sql",
    [
        "select count(*) from run_events",
        "  SELECT * FROM tenants WHERE repo = 'arsenyspb/ollama-monitor'",
        "with recent as (select 1) select * from recent",
        "WITH x AS (SELECT 1) SELECT * FROM x",
    ],
)
def test_validate_select_accepts_select_and_with(sql):
    assert db.validate_select(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "delete from tenants",
        "insert into run_events values (1)",
        "update tenants set status='x'",
        "drop table tenants",
        "select 1; drop table tenants",
        "select 1 -- ; delete from tenants",
    ],
)
def test_validate_select_rejects_non_select(sql):
    with pytest.raises(db.SqlRejected):
        db.validate_select(sql)


def test_validate_select_rejects_embedded_semicolon():
    with pytest.raises(db.SqlRejected):
        db.validate_select("select 1; select 2")


def test_wp4_db_tenant_includes_planner_manifest_sha_and_active_metrics(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.db,
        "tenant_row",
        lambda repo: {
            "id": "t-1",
            "repo": repo,
            "status": "active",
            "arbitration_mode": "recommend",
            "default_branch": "main",
            "manifest_sha": "abcdef1234567890",
        },
    )
    monkeypatch.setattr(cli.db, "active_metric_count", lambda tid: 3)
    monkeypatch.setattr(
        cli.db,
        "role_models",
        lambda tid: [
            {"role": "planner", "provider": "google", "model": "gemini-3-flash", "reasoning_effort": "low"},
            {"role": "developer", "provider": "openai", "model": "gpt-5", "reasoning_effort": "medium"},
        ],
    )
    monkeypatch.setattr(cli.db, "llm_keys_rows", lambda tid: [])
    monkeypatch.setattr(cli.creds, "kek_bytes", lambda: None)

    args = argparse.Namespace(repo="arsenyspb/ollama-monitor", timeout=15, json=False, verbose=False, max_chars=None)
    rc = cli.cmd_db_tenant(args)
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "manifest_sha=abcdef123456 active_metrics=3" in out
    assert "- planner    provider=google" in out
    assert "- developer  provider=openai" in out
    assert "- evaluator  <UNCONFIGURED>" in out
    assert "- arbitrator <UNCONFIGURED>" in out

    # When active_metric_count fails, show active_metrics=? rather than 0
    monkeypatch.setattr(cli.db, "active_metric_count", lambda tid: (_ for _ in ()).throw(RuntimeError("db error")))
    assert cli.cmd_db_tenant(args) == cli.EXIT_OK
    assert "active_metrics=?" in capsys.readouterr().out


def test_wp4_db_schema_tables_and_columns(monkeypatch, capsys):
    def fake_run_sql(sql, *, timeout=30):
        if "pg_class" in sql:
            return 0, " table_name | est_rows \n------------+----------\n tenants    |        4\n(1 row)\n", ""
        if "table_name = 'tenants'" in sql:
            return 0, " column_name | data_type | is_nullable \n-------------+-----------+-------------\n id          | uuid      | NO\n(1 row)\n", ""
        return 0, "(0 rows)\n", ""

    monkeypatch.setattr(db, "run_sql", fake_run_sql)
    monkeypatch.setattr(db, "list_public_tables", lambda *, timeout=10: ["tenants", "repository_wikis"])

    rc_all = cli.cmd_db_schema(argparse.Namespace(table=None, timeout=15, json=False, verbose=False, max_chars=None))
    assert rc_all == cli.EXIT_OK
    assert "tenants" in capsys.readouterr().out

    rc_one = cli.cmd_db_schema(
        argparse.Namespace(table="tenants", timeout=15, json=False, verbose=False, max_chars=None)
    )
    assert rc_one == cli.EXIT_OK
    assert "column_name" in capsys.readouterr().out

    rc_missing = cli.cmd_db_schema(
        argparse.Namespace(table="tenantz", timeout=15, json=False, verbose=False, max_chars=None)
    )
    out_missing = capsys.readouterr().out
    assert rc_missing == cli.EXIT_ERROR
    assert "Did you mean: tenants" in out_missing


def test_wp4_db_sql_suggests_real_columns_on_undefined_column(monkeypatch, capsys):
    def fake_run_sql(sql, *, timeout=30):
        if "commit_sha" in sql:
            return (
                1,
                "",
                'ERROR:  column "commit_sha" does not exist\nLINE 1: select commit_sha from repository_wikis\n',
            )
        return 0, "", ""

    monkeypatch.setattr(db, "run_sql", fake_run_sql)
    monkeypatch.setattr(
        db,
        "list_table_columns",
        lambda tbl, *, timeout=10: ["id", "repo", "tenant_id", "version_sha", "wiki_markdown", "updated_at"]
        if tbl == "repository_wikis"
        else [],
    )

    rc = cli.cmd_db_sql(
        argparse.Namespace(
            sql="select commit_sha from repository_wikis",
            timeout=15,
            json=False,
            verbose=False,
            max_chars=None,
        )
    )
    out = capsys.readouterr().out
    assert rc == cli.EXIT_ERROR
    assert "Did you mean:" in out
    assert "version_sha" in out
    assert "wiki_markdown" in out
