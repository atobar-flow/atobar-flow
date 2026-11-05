import argparse

import pytest

from atobar_ops import cli, resolve, state


@pytest.fixture(autouse=True)
def _fast_forward_deadline_sleep(monkeypatch):
    monkeypatch.setattr(
        cli.timebox.Deadline,
        "sleep",
        lambda self, chunk: setattr(self, "start", self.start - max(chunk, 1.0)),
    )


def _args(**kw):
    defaults = dict(repo="arsenyspb/ollama-monitor", issue=43, timeout=0.05, until="change", json=False, verbose=False, max_chars=None)
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _resolved(status, *, prs=None, instances=None, tenant=None, run_id=None):
    telemetry = resolve.TelemetryInfo(comment_id=1, status=status, tail="") if status is not None else None
    return resolve.Resolved(
        repo="arsenyspb/ollama-monitor",
        issue=43,
        tenant=tenant,
        telemetry=telemetry,
        instances=instances or [],
        prs=prs or [],
        run_id=run_id,
    )


def _patch_db(monkeypatch, *, events=(), rounds=(), roles=None):
    monkeypatch.setattr(cli.db, "run_events", lambda run_id, limit: list(events))
    monkeypatch.setattr(cli.db, "loop_rounds", lambda tenant_id=None, pr_number=None, limit=15: list(rounds))
    monkeypatch.setattr(cli.db, "session_roles_by_instance", lambda ids: dict(roles or {}))


ARBITRATION_VERDICT = {"kind": "verdict", "payload": {"mode": "shadow", "decision": "pass", "envelope": "arbitration"}}
EVALUATOR_VERDICT = {"kind": "verdict", "payload": {"verdict": "approved", "envelope": "verdict"}}


def test_watch_exits_immediately_when_status_differs_from_baseline(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    state.save("arsenyspb/ollama-monitor", 43, {"last_status": "🟢 Running (round 1)"})

    calls = {"n": 0}

    def fake_resolve(repo, issue, **kw):
        calls["n"] += 1
        return _resolved("🟢 Running (round 2)")

    monkeypatch.setattr(cli.resolve, "resolve", fake_resolve)

    rc = cli.cmd_watch(_args(timeout=30))
    out = capsys.readouterr().out

    assert rc == cli.EXIT_OK
    assert calls["n"] == 1  # returned on the very first internal poll, no spin
    assert "round 2" in out


def test_watch_stale_complete_status_is_not_terminal_while_instance_running(tmp_path, monkeypatch):
    # Each role posts its own telemetry comment. While the evaluator is running, the
    # *last* comment found can still be the developer's old "Complete" one until the
    # evaluator posts its own — that must not be read as the whole run being done.
    monkeypatch.chdir(tmp_path)
    state.save("arsenyspb/ollama-monitor", 43, {"last_status": "✅ Complete"})

    instance = resolve.InstanceInfo(instance_id="i-eval", role="evaluator", name="flow-evaluator-x-43", state="running")
    calls = {"n": 0}

    def fake_resolve(repo, issue, **kw):
        calls["n"] += 1
        return _resolved("✅ Complete", instances=[instance], tenant={"id": "t1"}, run_id="run1")

    monkeypatch.setattr(cli.resolve, "resolve", fake_resolve)
    _patch_db(monkeypatch)  # no arbitration verdict posted yet

    rc = cli.cmd_watch(_args(timeout=0.05, until="terminal"))
    assert rc == cli.EXIT_TIMEOUT  # ran out the clock, correctly did NOT call this terminal
    assert calls["n"] >= 1


def test_watch_complete_status_with_no_instance_is_terminal_once_arbitration_posted(tmp_path, monkeypatch):
    # The arbitrator is a deterministic gate with no LLM round, so it never writes a
    # loop_rounds row — the only reliable "the whole run is done" signal is the
    # flow-arbitration envelope's run_events row (kind="verdict", payload.envelope
    # == "arbitration"), not "latest loop_rounds role".
    monkeypatch.chdir(tmp_path)
    state.save("arsenyspb/ollama-monitor", 43, {"last_status": "🟢 Running"})

    def fake_resolve(repo, issue, **kw):
        return _resolved("✅ Complete", instances=[], tenant={"id": "t1"}, run_id="run1")

    monkeypatch.setattr(cli.resolve, "resolve", fake_resolve)
    _patch_db(monkeypatch, events=[EVALUATOR_VERDICT, ARBITRATION_VERDICT], rounds=[{"role": "evaluator"}])

    rc = cli.cmd_watch(_args(timeout=30, until="terminal"))
    assert rc == cli.EXIT_OK


def test_watch_complete_status_with_no_instance_is_not_terminal_before_arbitration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Baseline already matches the observed status, so this isolates the terminal
    # check from the separate "did anything change" signal.
    state.save("arsenyspb/ollama-monitor", 43, {"last_status": "✅ Complete"})

    def fake_resolve(repo, issue, **kw):
        return _resolved("✅ Complete", instances=[], tenant={"id": "t1"}, run_id="run1")

    monkeypatch.setattr(cli.resolve, "resolve", fake_resolve)
    _patch_db(monkeypatch, events=[EVALUATOR_VERDICT], rounds=[{"role": "evaluator"}])

    rc = cli.cmd_watch(_args(timeout=0.05, until="terminal"))
    assert rc == cli.EXIT_TIMEOUT


def test_watch_terminal_status_exits_immediately_even_if_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state.save("arsenyspb/ollama-monitor", 43, {"last_status": "✅ Complete"})

    calls = {"n": 0}

    def fake_resolve(repo, issue, **kw):
        calls["n"] += 1
        return _resolved("✅ Complete", tenant={"id": "t1"}, run_id="run1")

    monkeypatch.setattr(cli.resolve, "resolve", fake_resolve)
    _patch_db(monkeypatch, events=[ARBITRATION_VERDICT])

    rc = cli.cmd_watch(_args(timeout=30))
    assert rc == cli.EXIT_OK
    assert calls["n"] == 1


def test_watch_times_out_with_no_change(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state.save("arsenyspb/ollama-monitor", 43, {"last_status": "no telemetry"})

    def fake_resolve(repo, issue, **kw):
        return _resolved(None)  # -> status "no telemetry", matches baseline

    monkeypatch.setattr(cli.resolve, "resolve", fake_resolve)

    rc = cli.cmd_watch(_args(timeout=0.05))
    assert rc == cli.EXIT_TIMEOUT


def test_has_arbitration_verdict_ignores_evaluator_verdict():
    assert not cli._has_arbitration_verdict([EVALUATOR_VERDICT])
    assert cli._has_arbitration_verdict([EVALUATOR_VERDICT, ARBITRATION_VERDICT])
    assert not cli._has_arbitration_verdict([])


def test_watch_shows_active_session_role_for_rotated_warm_vm(tmp_path, monkeypatch, capsys):
    # A warm VM launched for the planner keeps its Name tag after rotating into the
    # developer role; the snapshot must show what is running now.
    monkeypatch.chdir(tmp_path)
    instance = resolve.InstanceInfo(instance_id="i-warm", role="planner", name="flow-planner-x-33", state="running")

    monkeypatch.setattr(
        cli.resolve, "resolve",
        lambda repo, issue, **kw: _resolved("🟢 Running", instances=[instance], tenant={"id": "t1"}, run_id="run1"),
    )
    _patch_db(monkeypatch, roles={"i-warm": "developer"})

    cli.cmd_watch(_args(timeout=0.05))
    out = capsys.readouterr().out

    assert "ec2[developer]: i-warm running (launched as planner)" in out
    assert "ec2[planner]" not in out


def test_watch_falls_back_to_launch_role_without_session_heartbeat(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    instance = resolve.InstanceInfo(instance_id="i-new", role="planner", name="flow-planner-x-33", state="pending")

    monkeypatch.setattr(
        cli.resolve, "resolve",
        lambda repo, issue, **kw: _resolved("🟢 Running", instances=[instance], tenant={"id": "t1"}, run_id="run1"),
    )
    _patch_db(monkeypatch, roles={})

    cli.cmd_watch(_args(timeout=0.05))
    assert "ec2[planner]: i-new pending\n" in capsys.readouterr().out
