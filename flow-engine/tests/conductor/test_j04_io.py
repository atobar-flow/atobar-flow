from unittest.mock import MagicMock

import httpx

from flow_engine.conductor.io import rebuild_loop_state
from flow_engine.conductor.machine import CheckCompletedEvent, VerdictEvent, step
from flow_engine.tenancy.manifest import LoopLimits


def test_j04_ac2_full_convergence():
    fake_gh = MagicMock()
    fake_gh.get_check_runs.return_value = [{"name": "flow-metrics", "conclusion": "success"}]
    fake_gh.get_pr.return_value = {"head": {"sha": "a" * 40}}

    fake_gh.get_comments.return_value = [
        {"id": 1, "user": {"login": "atobar-flow-agent[bot]"}, "body": (
            '```flow-verdict\n{"v":1,"role":"evaluator","tenant":"owner/repo","pr":42,"run_id":"r-eval",'
            f'"head_sha":"{"a" * 40}","round":1,"verdict":"approved","findings":[]}}\n```'
        )},
        {"id": 2, "user": {"login": "atobar-flow-agent[bot]"}, "body": (
            '```flow-ack\n{"v":1,"role":"developer","tenant":"owner/repo","pr":42,"run_id":"r-dev",'
            f'"head_sha":"{"a" * 40}","dispute":[]}}\n```'
        )}
    ]

    fake_store = MagicMock()
    fake_store.tenant_by_repo.return_value = MagicMock(id="t-1", manifest={})
    fake_store.loop_rounds_for_pr.return_value = []
    creds = {
        "r-eval": {"tenant_id": "t-1", "role": "evaluator", "head_sha": "a" * 40, "envelope_comment_id": 1},
        "r-dev": {"tenant_id": "t-1", "role": "developer", "head_sha": "a" * 40, "envelope_comment_id": 2},
    }
    fake_store.get_run_credential.side_effect = lambda rid: creds.get(rid)
    manifest_dict = {
        "repo": "owner/repo",
        "owner_github_login": "owner",
        "required_checks": ["flow-metrics"],
        "metrics": [{"key": "m1", "comparator": ">=", "threshold": 1.0}]
    }

    state = rebuild_loop_state(
        "owner/repo",
        42,
        fake_gh,
        fake_store,
        {
            "evaluator": "atobar-flow-agent[bot]",
            "developer": "atobar-flow-agent[bot]",
            "arbitrator": "atobar-flow-agent[bot]"
        },
        manifest_dict
    )

    assert state.required_checks == ["flow-metrics"]
    assert state.required_checks_status["flow-metrics"] == "success"

    event = CheckCompletedEvent(check_name="flow-metrics", conclusion="success", head_sha="a" * 40)
    new_state, actions = step(state, event, LoopLimits())
    assert new_state.status == "converged"

def test_j04_ac3_issue_only_payload_handled_gracefully():
    fake_gh = MagicMock()
    class Fake404Response:
        status_code = 404
    class FakeHTTPError(httpx.HTTPStatusError):
        def __init__(self):
            self.response = Fake404Response()

    fake_gh.get_pr.side_effect = FakeHTTPError()
    fake_gh.get_comments.return_value = []
    fake_store = MagicMock()

    state = rebuild_loop_state(
        "owner/repo",
        42,
        fake_gh,
        fake_store,
        {},
        {"required_checks": [], "metrics": [{"key": "m1", "comparator": ">=", "threshold": 1.0}]}
    )
    assert state.head_sha == ""

def test_j04_ac4_stale_event_ignored():
    fake_gh = MagicMock()
    fake_gh.get_pr.return_value = {"head": {"sha": "current_sha"}}
    fake_gh.get_comments.return_value = []
    fake_store = MagicMock()

    state = rebuild_loop_state(
        "owner/repo",
        42,
        fake_gh,
        fake_store,
        {},
        {}
    )

    event = CheckCompletedEvent(check_name="flow-metrics", conclusion="success", head_sha="old_sha")
    new_state, actions = step(state, event, LoopLimits())
    assert new_state.required_checks_status == {}

def test_j04_ac1_limits_manifest_mapping():
    fake_gh = MagicMock()
    fake_gh.get_pr.return_value = {"head": {"sha": "a" * 40}}
    fake_gh.get_comments.return_value = []
    fake_store = MagicMock()

    state = rebuild_loop_state(
        "owner/repo",
        42,
        fake_gh,
        fake_store,
        {},
        {}
    )

    from atobar_contracts.envelopes import Verdict
    v_event = VerdictEvent(
        verdict=Verdict(
            tenant="owner/repo", pr=42, head_sha="a" * 40, round=1, verdict="changes_requested", findings=[]
        ),
        cost_usd=0.5
    )

    limits = LoopLimits(max_review_rounds=5, max_pr_cost_usd=0.5)
    new_state, actions = step(state, v_event, limits)

    assert new_state.status == "escalated"
    assert "max_pr_cost_usd" in new_state.escalation_reason


def test_real_github_client_dispatch_respects_event_type(monkeypatch):
    from flow_engine.conductor.io import RealGitHubClient

    posted: list[dict] = []

    class FakeResp:
        def raise_for_status(self):
            pass

    def fake_post(url, headers=None, json=None, timeout=None):
        posted.append({"url": url, "json": json})
        return FakeResp()

    monkeypatch.setattr(httpx, "post", fake_post)
    client = RealGitHubClient(token="tok")

    client.dispatch_flow_run("owner/repo", {"role": "developer", "pr": 42})
    assert posted[-1]["json"]["event_type"] == "flow_run"

    client.dispatch_flow_run(
        "owner/repo",
        {"event_type": "flow_event", "tenant": "owner/repo", "event": {"kind": "issue_comment.created"}},
    )
    assert posted[-1]["json"]["event_type"] == "flow_event"


def test_apply_actions_assigns_and_unassigns_configured_human():
    from atobar_contracts.conductor_io import apply_actions
    from atobar_contracts.machine import EscalateAction, RemoveLabelAction, SetLabelAction
    from atobar_contracts.store import Tenant

    fake_gh = MagicMock()
    fake_store = MagicMock()
    fake_store.tenant_by_repo.return_value = Tenant(
        id="t-1",
        repo="owner/repo",
        status="active",
        arbitration_mode="recommend",
        manifest={"repo": "owner/repo", "owner_github_login": "collab-alice", "loop": {"max_pr_cost_usd": 3.0}},
    )

    # 1. Entering flow:needs-approval assigns collab-alice and posts status card
    apply_actions("owner/repo", 15, [SetLabelAction(label="flow:needs-approval")], fake_gh, fake_store)
    fake_gh.add_assignees.assert_called_with("owner/repo", 15, ["collab-alice"])
    assert any("flow:approved" in str(c.args[2]) for c in fake_gh.create_comment.call_args_list)

    # 2. Removing flow:needs-approval unassigns collab-alice
    apply_actions("owner/repo", 15, [RemoveLabelAction(label="flow:needs-approval")], fake_gh, fake_store)
    fake_gh.remove_assignees.assert_called_with("owner/repo", 15, ["collab-alice"])

    # 3. EscalateAction assigns collab-alice and includes budget reset & cancel instructions
    fake_gh.reset_mock()
    apply_actions(
        "owner/repo",
        15,
        [EscalateAction(reason="max_pr_cost_usd reached ($3.10 >= $3.00)", circuit_breaker="max_pr_cost_usd")],
        fake_gh,
        fake_store,
    )
    fake_gh.add_assignees.assert_called_with("owner/repo", 15, ["collab-alice"])
    comment_body = fake_gh.create_comment.call_args.args[2]
    assert "Circuit breaker tripped: `max_pr_cost_usd`" in comment_body
    assert "Applying `flow:resume` starts a fresh loop epoch" in comment_body
    assert "Close this issue to cancel and stop compute" in comment_body

