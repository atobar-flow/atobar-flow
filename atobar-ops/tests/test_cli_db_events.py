import argparse

from atobar_ops import cli


def _args(**kw):
    defaults = dict(repo="arsenyspb/ollama-monitor", issue=None, run=None, limit=30, timeout=5, json=False, verbose=False, max_chars=None)
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_db_events_issue_filters_loop_rounds_by_dispatch_id(monkeypatch, capsys):
    # loop_rounds.pr_number is populated from the conductor's dispatch id, which is
    # the GitHub *issue* number (see dispatch.py's FLOW_PR/ISSUE_ID), not the PR
    # number that eventually gets opened.
    monkeypatch.setattr(cli.db, "tenant_row", lambda repo: {"id": "tenant-1"})
    monkeypatch.setattr(cli.db, "latest_run_id", lambda tid: "run_latest")
    monkeypatch.setattr(cli.db, "run_events", lambda run_id, limit: [])

    captured = {}

    def fake_loop_rounds(tenant_id=None, pr_number=None, limit=15):
        captured["tenant_id"] = tenant_id
        captured["pr_number"] = pr_number
        return []

    monkeypatch.setattr(cli.db, "loop_rounds", fake_loop_rounds)

    rc = cli.cmd_db_events(_args(issue=43))
    out = capsys.readouterr().out

    assert rc == cli.EXIT_OK
    assert captured["tenant_id"] == "tenant-1"
    assert captured["pr_number"] == 43
    assert "issue=43" in out


def test_db_events_without_issue_does_not_filter_by_pr(monkeypatch):
    monkeypatch.setattr(cli.db, "tenant_row", lambda repo: {"id": "tenant-1"})
    monkeypatch.setattr(cli.db, "latest_run_id", lambda tid: "run_latest")
    monkeypatch.setattr(cli.db, "run_events", lambda run_id, limit: [])

    captured = {}

    def fake_loop_rounds(tenant_id=None, pr_number=None, limit=15):
        captured["pr_number"] = pr_number
        return []

    monkeypatch.setattr(cli.db, "loop_rounds", fake_loop_rounds)

    cli.cmd_db_events(_args(issue=None))
    assert captured["pr_number"] is None


def test_db_events_formats_runtime_wall_clock_calls_and_arbitration_decisions(monkeypatch, capsys):
    monkeypatch.setattr(cli.db, "tenant_row", lambda repo: {"id": "tenant-1"})
    monkeypatch.setattr(cli.db, "latest_run_id", lambda tid: "run_latest")
    monkeypatch.setattr(cli.db, "run_events", lambda run_id, limit: [])
    monkeypatch.setattr(
        cli.db,
        "loop_rounds",
        lambda tenant_id=None, pr_number=None, limit=15: [
            {
                "pr_number": 43,
                "round": 0,
                "role": "developer",
                "tokens": 12345,
                "cost_usd": 0.0123,
                "wall_clock_minutes": 1.45,
                "usage_detail": {
                    "agent_runtime": "nanobot",
                    "calls": [{"iteration": 1, "input_tokens": 10000, "output_tokens": 2345}],
                },
            }
        ],
    )
    monkeypatch.setattr(
        cli.db,
        "arbitration_decisions",
        lambda tenant_id=None, pr_number=None, limit=5: [
            {
                "pr_number": 43,
                "mode": "recommend",
                "decision": "pass",
                "manifest_sha": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
            }
        ],
    )

    rc = cli.cmd_db_events(_args(issue=43))
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "runtime=nanobot" in out
    assert "wc=1.45m" in out
    assert "calls=1" in out
    assert "ARBITRATION_DECISIONS" in out
    assert "mode=recommend decision=pass manifest_sha=abcdef012345" in out


def test_cp_probe_validates_healthz_docs_openapi_and_screenshots(monkeypatch, capsys):
    import httpx

    def fake_get(url, timeout=5, follow_redirects=False):
        req = httpx.Request("GET", url)
        if url.endswith("/healthz"):
            return httpx.Response(200, json={"status": "ok"}, request=req)
        if url.endswith("/docs") or url.endswith("/openapi.json"):
            return httpx.Response(404, text="Not Found", request=req)
        if "/screenshots/" in url:
            return httpx.Response(401, json={"detail": "Not authenticated"}, request=req)
        if url.endswith("/login"):
            return httpx.Response(302, headers={"location": "https://github.com/login/oauth/authorize"}, request=req)
        if "/tenants/" in url:
            return httpx.Response(303, headers={"location": "/login"}, request=req)
        return httpx.Response(200, text="OK", request=req)

    monkeypatch.setattr(httpx, "get", fake_get)
    rc = cli.cmd_cp_probe(_args())
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert '200 /healthz {"status":"ok"}' in out
    assert "404 /docs" in out
    assert "404 /openapi.json" in out
    assert "401 /v1/runs/nonexistent-run/screenshots/nonexistent.png" in out


def test_cp_probe_flags_exposed_docs_or_unscrubbed_healthz(monkeypatch, capsys):
    import httpx

    def fake_get(url, timeout=5, follow_redirects=False):
        req = httpx.Request("GET", url)
        if url.endswith("/healthz"):
            return httpx.Response(200, json={"status": "ok", "supabase": "leaked"}, request=req)
        if url.endswith("/docs"):
            return httpx.Response(200, text="Swagger UI", request=req)
        if url.endswith("/openapi.json"):
            return httpx.Response(404, text="Not Found", request=req)
        if "/screenshots/" in url:
            return httpx.Response(200, content=b"png", request=req)
        return httpx.Response(200, text="OK", request=req)

    monkeypatch.setattr(httpx, "get", fake_get)
    rc = cli.cmd_cp_probe(_args())
    out = capsys.readouterr().out
    assert rc == cli.EXIT_ERROR
    assert "FLAG: /healthz" in out
    assert "FLAG: /docs returned 200" in out
    assert "FLAG: /v1/runs/nonexistent-run/screenshots/nonexistent.png returned 200" in out

