import os
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from atobar_contracts.envelopes import Ack, Plan, PlannedCriterion, Verdict, render
from flow_agent.roles.base import TurnOutcome
from flow_agent.runtime.sandbox import Workspace

SHA1 = "1111111111111111111111111111111111111111"
SHA2 = "2222222222222222222222222222222222222222"
SENTINEL = "0" * 40


@pytest.mark.asyncio
async def test_wp5_ac1_ac2_supervisor_loops_across_roles_and_scrubs_keys(tmp_path):
    from flow_agent.main import main

    executed_turns: list[dict[str, Any]] = []

    async def fake_run_planner(**kwargs: Any) -> TurnOutcome:
        os.environ["FLOW_LLM_API_KEY"] = "planner-secret-key"
        os.environ["FLOW_LLM_API_KEY_GEMINI"] = "planner-gemini-key"
        executed_turns.append({
            "role": "planner",
            "env_role": os.environ.get("FLOW_ROLE"),
            "run_id": os.environ.get("FLOW_RUN_ID"),
            "workspace_dir": os.environ.get("WORKSPACE_DIR"),
        })
        plan = Plan(
            tenant="arsenyspb/ollama-monitor",
            pr=33,
            head_sha=SENTINEL,
            run_id=os.environ.get("FLOW_RUN_ID"),
            complexity="single_pr",
            goal="Fix overflow",
            scope_files=["src/ui.py"],
            acceptance_criteria=[PlannedCriterion(id="AC-1", description="Fits 80 cols")],
            implementation_steps=["Clamp width"],
        )
        return TurnOutcome(envelope_text=render(plan), comment_id=101, head_sha=SENTINEL)

    async def fake_run_developer(**kwargs: Any) -> TurnOutcome:
        # Prior role's LLM keys must have been scrubbed before this turn started
        assert "FLOW_LLM_API_KEY" not in os.environ
        assert "FLOW_LLM_API_KEY_GEMINI" not in os.environ
        os.environ["FLOW_LLM_API_KEY"] = "dev-secret-key"
        executed_turns.append({
            "role": "developer",
            "env_role": os.environ.get("FLOW_ROLE"),
            "run_id": os.environ.get("FLOW_RUN_ID"),
            "workspace_dir": os.environ.get("WORKSPACE_DIR"),
            "latest_plan": kwargs.get("latest_plan"),
        })
        ack = Ack(
            tenant="arsenyspb/ollama-monitor",
            pr=33,
            head_sha=SHA1,
            run_id=os.environ.get("FLOW_RUN_ID"),
        )
        return TurnOutcome(envelope_text=render(ack), comment_id=102, head_sha=SHA1)

    async def fake_run_evaluator(**kwargs: Any) -> TurnOutcome:
        assert "FLOW_LLM_API_KEY" not in os.environ
        os.environ["FLOW_LLM_API_KEY"] = "eval-secret-key"
        executed_turns.append({
            "role": "evaluator",
            "env_role": os.environ.get("FLOW_ROLE"),
            "run_id": os.environ.get("FLOW_RUN_ID"),
            "head_sha": kwargs.get("head_sha"),
            "workspace_dir": os.environ.get("WORKSPACE_DIR"),
            "latest_plan": kwargs.get("latest_plan"),
        })
        verdict = Verdict(
            tenant="arsenyspb/ollama-monitor",
            pr=33,
            head_sha=SHA1,
            run_id=os.environ.get("FLOW_RUN_ID"),
            round=1,
            verdict="approved",
            findings=[],
        )
        return TurnOutcome(envelope_text=render(verdict), comment_id=103, head_sha=SHA1)

    handover_responses = [
        {
            "action": "continue",
            "role": "developer",
            "run_id": "run_dev_1",
            "run_credential": "cred_dev_1",
            "head_sha": SENTINEL,
        },
        {
            "action": "continue",
            "role": "evaluator",
            "run_id": "run_eval_1",
            "run_credential": "cred_eval_1",
            "head_sha": SHA1,
        },
        {
            "action": "shutdown",
            "reason": "converged",
        },
    ]

    def fake_prepare_worktree(**kwargs: Any) -> Workspace:
        role = kwargs.get("role", "developer")
        run_id = kwargs.get("run_id", "run")
        ref = kwargs.get("ref", "main")
        wt = tmp_path / f"wt-{role}-{run_id[:8]}"
        wt.mkdir(parents=True, exist_ok=True)
        os.environ["WORKSPACE_DIR"] = str(wt)
        return Workspace(
            repo="arsenyspb/ollama-monitor",
            ref=ref,
            path=wt,
            commit_sha=ref if len(ref) == 40 else SHA1,
        )

    env_vars = {
        "FLOW_ROLE": "planner",
        "FLOW_RUN_ID": "run_plan_1",
        "FLOW_RUN_CREDENTIAL": "cred_plan_1",
        "FLOW_HEAD_SHA": SENTINEL,
        "TARGET_REPO": "arsenyspb/ollama-monitor",
        "TARGET_REF": "main",
        "ISSUE_ID": "33",
        "ISSUE_TEXT": "Fix dashboard overflow",
        "GITHUB_TOKEN": "gh-tok",
        "CONTROL_PANEL_URL": "http://fake-cp",
        "INSTANCE_ID": "i-warm999",
    }

    fake_gh = MagicMock()
    fake_gh.get_comments.return_value = []

    with patch.dict(os.environ, env_vars, clear=True):
        with patch("flow_agent.main.prepare_role_worktree", side_effect=fake_prepare_worktree), \
             patch("flow_agent.main.RealGitHubClient", return_value=fake_gh), \
             patch("flow_agent.main.run_planner", side_effect=fake_run_planner), \
             patch("flow_agent.main.run_developer", side_effect=fake_run_developer), \
             patch("flow_agent.main.run_evaluator", side_effect=fake_run_evaluator), \
             patch("flow_agent.main._report_turn_complete", side_effect=handover_responses) as mock_tc, \
             patch("flow_agent.main._shutdown", new_callable=AsyncMock) as mock_shutdown, \
             patch("flow_agent.main.SessionRegistry", autospec=True), \
             patch("flow_agent.main.GitHubTelemetryUI", autospec=True), \
             patch("flow_agent.main.fetch_run_wiki", return_value={"purpose": "Monitor Ollama"}), \
             patch("argparse.ArgumentParser.parse_known_args", return_value=(MagicMock(role="planner"), [])):
            await main()

    assert [t["role"] for t in executed_turns] == ["planner", "developer", "evaluator"]
    assert [t["run_id"] for t in executed_turns] == ["run_plan_1", "run_dev_1", "run_eval_1"]
    assert len({t["workspace_dir"] for t in executed_turns}) == 3
    # Plan emitted by planner was automatically propagated into developer & evaluator turns
    assert executed_turns[1]["latest_plan"] is not None
    assert executed_turns[1]["latest_plan"].goal == "Fix overflow"
    assert executed_turns[2]["latest_plan"] is not None
    assert executed_turns[2]["head_sha"] == SHA1
    assert mock_tc.call_count == 3
    mock_shutdown.assert_called_once()
    assert mock_shutdown.call_args[1].get("error") is False


@pytest.mark.asyncio
async def test_wp5_ac3_fallback_to_single_shot_shutdown_when_turn_complete_unavailable(tmp_path):
    from flow_agent.main import main

    async def fake_run_developer(**kwargs: Any) -> TurnOutcome:
        ack = Ack(tenant="arsenyspb/ollama-monitor", pr=33, head_sha=SHA1, run_id="run_dev_1")
        return TurnOutcome(envelope_text=render(ack), comment_id=201, head_sha=SHA1)

    def fake_prepare_worktree(**kwargs: Any) -> Workspace:
        wt = tmp_path / "wt-dev"
        wt.mkdir(parents=True, exist_ok=True)
        return Workspace(repo="arsenyspb/ollama-monitor", ref=SHA1, path=wt, commit_sha=SHA1)

    env_vars = {
        "FLOW_ROLE": "developer",
        "FLOW_RUN_ID": "run_dev_1",
        "FLOW_RUN_CREDENTIAL": "cred_dev_1",
        "TARGET_REPO": "arsenyspb/ollama-monitor",
        "TARGET_REF": SHA1,
        "ISSUE_ID": "33",
        "GITHUB_TOKEN": "gh-tok",
        "CONTROL_PANEL_URL": "http://fake-cp",
    }

    fake_gh = MagicMock()
    fake_gh.get_comments.return_value = []

    with patch.dict(os.environ, env_vars, clear=True):
        with patch("flow_agent.main.prepare_role_worktree", side_effect=fake_prepare_worktree), \
             patch("flow_agent.main.RealGitHubClient", return_value=fake_gh), \
             patch("flow_agent.main.run_developer", side_effect=fake_run_developer) as mock_dev, \
             patch("flow_agent.main._report_turn_complete", return_value={"action": "shutdown", "reason": "unreachable"}), \
             patch("flow_agent.main._shutdown", new_callable=AsyncMock) as mock_shutdown, \
             patch("flow_agent.main.SessionRegistry", autospec=True), \
             patch("flow_agent.main.GitHubTelemetryUI", autospec=True), \
             patch("flow_agent.main.fetch_run_wiki", return_value=None), \
             patch("argparse.ArgumentParser.parse_known_args", return_value=(MagicMock(role="developer"), [])):
            await main()

    assert mock_dev.call_count == 1
    mock_shutdown.assert_called_once()
    assert mock_shutdown.call_args[1].get("error") is False


@pytest.mark.asyncio
async def test_metric_compiler_role_gets_key_and_decoded_intent_then_shuts_down(tmp_path):
    import base64

    from flow_agent.main import main

    seen: dict[str, Any] = {}

    async def fake_compiler(**kwargs: Any) -> TurnOutcome:
        seen.update(kwargs)
        return TurnOutcome(envelope_text="", head_sha=SHA1)

    def fake_prepare_worktree(**kwargs: Any) -> Workspace:
        seen["prepare_ref"] = kwargs.get("ref")
        wt = tmp_path / "wt-metric"
        wt.mkdir(parents=True, exist_ok=True)
        os.environ["WORKSPACE_DIR"] = str(wt)
        return Workspace(repo="arsenyspb/ollama-monitor", ref="master", path=wt, commit_sha=SHA1)

    intent = 'No "proxy" process; $(rm -rf /) stays text'
    env_vars = {
        "FLOW_ROLE": "metric_compiler",
        "FLOW_RUN_ID": "run_c__proxy_count",
        "FLOW_RUN_CREDENTIAL": "cred_c",
        "FLOW_METRIC_KEY": "proxy_count",
        "FLOW_METRIC_INTENT_B64": base64.b64encode(intent.encode()).decode(),
        "TARGET_REPO": "arsenyspb/ollama-monitor",
        "TARGET_REF": "master",
        "ISSUE_ID": "0",
        "GITHUB_TOKEN": "gh-tok",
        "CONTROL_PANEL_URL": "http://fake-cp",
    }
    with patch.dict(os.environ, env_vars, clear=True):
        with patch("flow_agent.main.prepare_role_worktree", side_effect=fake_prepare_worktree), \
             patch("flow_agent.main.RealGitHubClient", return_value=MagicMock()), \
             patch("flow_agent.main.run_metric_compiler", side_effect=fake_compiler), \
             patch("flow_agent.main.WipSyncer") as mock_syncer, \
             patch("flow_agent.main._report_turn_complete") as mock_tc, \
             patch("flow_agent.main._shutdown", new_callable=AsyncMock) as mock_shutdown, \
             patch("flow_agent.main.SessionRegistry", autospec=True), \
             patch("flow_agent.main.GitHubTelemetryUI", autospec=True), \
             patch("flow_agent.main.fetch_run_wiki", return_value=None), \
             patch("argparse.ArgumentParser.parse_known_args", return_value=(MagicMock(role="metric_compiler"), [])):
            await main()

    assert seen["key"] == "proxy_count"
    assert seen["intent"] == intent
    assert seen["workspace"] == str(tmp_path / "wt-metric")
    assert seen["prepare_ref"] == "master"
    mock_syncer.assert_not_called()
    mock_tc.assert_not_called()
    mock_shutdown.assert_called_once()
    assert mock_shutdown.call_args[1].get("error") is False


def test_decode_metric_intent_tolerates_garbage():
    from flow_agent.main import _decode_metric_intent

    assert _decode_metric_intent("") == ""
    assert _decode_metric_intent("!!not-base64!!") == ""
    assert _decode_metric_intent("//79") == ""  # valid base64, invalid UTF-8


@pytest.mark.asyncio
async def test_supervisor_halt_outcome_propagates_error_to_shutdown(tmp_path):
    from flow_agent.main import main

    async def fake_run_developer(**kwargs: Any) -> TurnOutcome:
        return TurnOutcome(
            envelope_text="",
            head_sha=SENTINEL,
            halt="provider_error",
            halt_detail="provider_error: model=gemini-nonexistent status=404",
        )

    def fake_prepare_worktree(**kwargs: Any) -> Workspace:
        wt = tmp_path / "wt-dev-halt"
        wt.mkdir(parents=True, exist_ok=True)
        return Workspace(repo="arsenyspb/ollama-monitor", ref="main", path=wt, commit_sha=SHA1)

    env_vars = {
        "FLOW_ROLE": "developer",
        "FLOW_RUN_ID": "run_dev_halt",
        "FLOW_RUN_CREDENTIAL": "cred_dev_halt",
        "TARGET_REPO": "arsenyspb/ollama-monitor",
        "TARGET_REF": "main",
        "ISSUE_ID": "43",
        "GITHUB_TOKEN": "gh-tok",
        "CONTROL_PANEL_URL": "http://fake-cp",
    }

    with patch.dict(os.environ, env_vars, clear=True):
        with patch("flow_agent.main.prepare_role_worktree", side_effect=fake_prepare_worktree), \
             patch("flow_agent.main.RealGitHubClient", return_value=MagicMock()), \
             patch("flow_agent.main.run_developer", side_effect=fake_run_developer), \
             patch("flow_agent.main._report_turn_complete") as mock_tc, \
             patch("flow_agent.main._shutdown", new_callable=AsyncMock) as mock_shutdown, \
             patch("flow_agent.main.SessionRegistry", autospec=True), \
             patch("flow_agent.main.GitHubTelemetryUI", autospec=True), \
             patch("flow_agent.main.fetch_run_wiki", return_value=None), \
             patch("argparse.ArgumentParser.parse_known_args", return_value=(MagicMock(role="developer"), [])):
            await main()

    mock_tc.assert_not_called()
    mock_shutdown.assert_called_once()
    assert mock_shutdown.call_args[0][0] == "provider_error: model=gemini-nonexistent status=404"
    assert mock_shutdown.call_args[1].get("error") is True


def test_report_turn_complete_includes_wall_clock_minutes():
    from flow_agent.main import _report_turn_complete

    captured_json: dict[str, Any] = {}

    def fake_post(url: str, headers: dict[str, str], json: dict[str, Any], timeout: float) -> Any:
        captured_json.update(json)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"action": "shutdown", "reason": "complete"}
        return resp

    outcome = TurnOutcome(
        envelope_text="```flow-ack\n{}\n```",
        comment_id=77,
        head_sha=SHA1,
        usage={"agent_runtime": "nanobot", "total": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001}, "calls": []},
        wall_clock_minutes=1.2345,
    )
    with patch("flow_agent.main.httpx.post", side_effect=fake_post):
        res = _report_turn_complete(
            cp_url="https://cp.example.invalid",
            run_id="run_1",
            run_cred="cred_1",
            turn_outcome=outcome,
            issue_id=43,
            instance_id="i-123",
        )
    assert res["action"] == "shutdown"
    assert captured_json["wall_clock_minutes"] == 1.2345
    assert captured_json["usage"]["agent_runtime"] == "nanobot"


@pytest.mark.asyncio
async def test_run_arbitrator_turn_polls_until_metric_observations_arrive():
    from flow_agent.main import _run_arbitrator_turn

    responses = [
        # First poll: PR observation not yet uploaded by flow-metrics.yml
        {
            "tenant_id": "t-1",
            "arbitration_mode": "shadow",
            "manifest": {
                "repo": "arsenyspb/ollama-monitor",
                "owner_github_login": "arsenyspb",
                "metrics": [{"key": "dependency_count", "comparator": "<=", "threshold": 0.0, "baseline_mode": "no_regression"}],
            },
            "recent_observations": [
                {"key": "dependency_count", "value": 9.0, "commit_sha": SHA2, "pr_number": None, "workflow_path": ".github/workflows/flow-metrics.yml"},
            ],
        },
        # Second poll: PR observation arrived
        {
            "tenant_id": "t-1",
            "arbitration_mode": "shadow",
            "manifest": {
                "repo": "arsenyspb/ollama-monitor",
                "owner_github_login": "arsenyspb",
                "metrics": [{"key": "dependency_count", "comparator": "<=", "threshold": 0.0, "baseline_mode": "no_regression"}],
            },
            "recent_observations": [
                {"key": "dependency_count", "value": 9.0, "commit_sha": SHA1, "pr_number": 50, "workflow_path": ".github/workflows/flow-metrics.yml"},
                {"key": "dependency_count", "value": 9.0, "commit_sha": SHA2, "pr_number": None, "workflow_path": ".github/workflows/flow-metrics.yml"},
            ],
        },
    ]
    call_idx = {"n": 0}

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> Any:
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        idx = min(call_idx["n"], len(responses) - 1)
        call_idx["n"] += 1
        resp.json.return_value = responses[idx]
        return resp

    fake_gh = MagicMock()
    fake_gh.get_commit_sha.return_value = SHA2
    fake_gh.find_pr_for_sha.return_value = 50
    fake_gh.get_pr_files.return_value = ["src/monitor.py"]

    env_vars = {
        "CONTROL_PANEL_URL": "https://cp.example.invalid",
        "FLOW_RUN_ID": "run_arb_1",
        "FLOW_RUN_CREDENTIAL": "cred_arb_1",
        "FLOW_ARBITRATOR_OBS_WAIT_SECONDS": "10",
    }
    with patch.dict(os.environ, env_vars, clear=False), \
         patch("flow_agent.main.httpx.get", side_effect=fake_get), \
         patch("flow_agent.main.httpx.post") as mock_post, \
         patch("flow_agent.main.asyncio.sleep", new_callable=AsyncMock):
        mock_post.return_value.raise_for_status = MagicMock()
        outcome = await _run_arbitrator_turn(
            target_repo="arsenyspb/ollama-monitor",
            issue_id=43,
            target_ref=SHA1,
            gh_client=fake_gh,
        )

    assert call_idx["n"] == 2
    assert '"decision":"pass"' in outcome.envelope_text or '"decision": "pass"' in outcome.envelope_text

