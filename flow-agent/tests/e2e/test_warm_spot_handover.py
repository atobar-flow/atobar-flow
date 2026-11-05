import dataclasses
import os
import subprocess
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from control_panel.app import app, current_tenant, get_dispatcher, get_store
from control_panel.store import MemoryStore, Tenant
from flow_agent.main import main
from atobar_contracts.envelopes import (
    Ack,
    Finding,
    Plan,
    PlannedCriterion,
    Verdict,
    render,
)
from flow_agent.roles.base import TurnOutcome
from flow_agent.runtime.sandbox import prepare_role_worktree


@pytest.fixture(autouse=True)
def _clean_workspace_dir_env():
    prev = os.environ.pop("WORKSPACE_DIR", None)
    yield
    if prev is None:
        os.environ.pop("WORKSPACE_DIR", None)
    else:
        os.environ["WORKSPACE_DIR"] = prev


class _E2EDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[tuple[str, dict[str, Any]]] = []
        self.issue_bodies: dict[tuple[str, int], str] = {}
        self.comments: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.labels: dict[tuple[str, int], set[str]] = {}
        self.check_runs: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def dispatch(self, event_type: str, client_payload: dict[str, Any]) -> None:
        self.dispatched.append((event_type, client_payload))

    def get_issue_body(self, repo: str, issue_number: int) -> str:
        return self.issue_bodies.get((repo, issue_number), "")

    def get_collaborator_permission(self, repo: str, username: str) -> str | None:
        return "admin"

    def remove_issue_label(self, repo: str, issue_number: int, label: str) -> None:
        self.labels.setdefault((repo, issue_number), set()).discard(label)

    def add_issue_label(self, repo: str, issue_number: int, label: str) -> None:
        self.labels.setdefault((repo, issue_number), set()).add(label)

    def create_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
        self.comments.setdefault((repo, issue_number), []).append(
            {"user": {"login": "atobar-flow-agent[bot]"}, "body": body}
        )

    def comment_on_open_issue_by_label(self, repo: str, label: str, comment: str) -> bool:
        return True

    def comment_on_manifest_pr(self, repo: str, comment: str) -> bool:
        return True


def _init_git_origin(tmp_path) -> tuple[Any, str, str]:
    origin_dir = tmp_path / "origin"
    origin_dir.mkdir()
    subprocess.run(["git", "init"], cwd=origin_dir, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=origin_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=origin_dir, check=True)

    (origin_dir / "app.py").write_text("v1\n")
    subprocess.run(["git", "add", "."], cwd=origin_dir, check=True)
    subprocess.run(["git", "commit", "-m", "v1"], cwd=origin_dir, check=True, stdout=subprocess.DEVNULL)
    sha1 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=origin_dir, text=True).strip()

    (origin_dir / "app.py").write_text("v2\n")
    subprocess.run(["git", "add", "."], cwd=origin_dir, check=True)
    subprocess.run(["git", "commit", "-m", "v2"], cwd=origin_dir, check=True, stdout=subprocess.DEVNULL)
    sha2 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=origin_dir, text=True).strip()
    return origin_dir, sha1, sha2


@pytest.mark.asyncio
async def test_e2e_fast_track_trajectory_2_rounds_on_single_warm_supervisor(tmp_path):
    """Fast-track trajectory: well-specified issue -> dev R1 -> eval R1 (changes_requested)
    -> dev R2 -> eval R2 (approved) -> arbitrator on 1 warm supervisor with isolated worktrees.
    """
    origin_dir, sha1, sha2 = _init_git_origin(tmp_path)
    base_repo = tmp_path / "repo-base"
    wt_root = tmp_path / "worktrees"

    store = MemoryStore()
    tenant = store.create_tenant("arsenyspb/ollama-monitor", "arsenyspb")
    store.update_tenant_status(tenant.id, "active")
    store.tenants[tenant.id] = dataclasses.replace(
        store.tenants[tenant.id],
        manifest={"repo": "arsenyspb/ollama-monitor", "required_checks": [], "loop": {"max_review_rounds": 4}},
    )
    store.wikis[tenant.id] = {"purpose": "Ollama TUI monitor"}

    disp = _E2EDispatcher()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_dispatcher] = lambda: disp
    app.dependency_overrides[current_tenant] = lambda: store.tenants[tenant.id]
    cp_client = TestClient(app)

    try:
        issue_num = 33
        well_specified_body = (
            "## Scope\n- `src/ui/dashboard.py`\n\n"
            "## Acceptance criteria\n- [ ] AC-1: Dashboard does not overflow at 80 cols.\n"
        )
        disp.issue_bodies[(tenant.repo, issue_num)] = well_specified_body

        # 1. Ingress on POST /v1/events fast-tracks straight to developer
        r_init = cp_client.post(
            "/v1/events",
            json={"kind": "issues.labeled", "issue_number": issue_num, "label": "flow:feature"},
        )
        assert r_init.status_code == 202 and r_init.json()["dispatched"] is True
        assert len(disp.dispatched) == 1
        wf, init_payload = disp.dispatched[0]
        assert wf == "flow_run"
        assert init_payload["role"] == "developer"

        # Clear initial boot dispatch; zero additional dispatches may occur during the warm loop!
        disp.dispatched.clear()

        worktrees_seen: list[tuple[str, str]] = []
        dev_round = 0
        eval_round = 0

        def real_worktree_wrapper(**kwargs: Any):
            kwargs["clone_url"] = str(origin_dir)
            kwargs["setup_cmds"] = []
            ws = prepare_role_worktree(**kwargs)
            worktrees_seen.append((kwargs["role"], str(ws.path)))
            return ws

        async def fake_run_developer(**kwargs: Any) -> TurnOutcome:
            nonlocal dev_round
            dev_round += 1
            ws_path = pathlib.Path(os.environ["WORKSPACE_DIR"])
            (ws_path / "memory").mkdir(parents=True, exist_ok=True)
            (ws_path / "memory" / "history.jsonl").write_text(f"dev round {dev_round}\n")
            sha = sha1 if dev_round == 1 else sha2
            ack = Ack(
                tenant=tenant.repo,
                pr=issue_num,
                head_sha=sha,
                run_id=os.environ["FLOW_RUN_ID"],
            )
            return TurnOutcome(envelope_text=render(ack), comment_id=100 + dev_round, head_sha=sha)

        async def fake_run_evaluator(**kwargs: Any) -> TurnOutcome:
            nonlocal eval_round
            eval_round += 1
            ws_path = pathlib.Path(os.environ["WORKSPACE_DIR"])
            # Axiom 11: Evaluator worktree must never contain developer's memory/history.jsonl
            assert not (ws_path / "memory" / "history.jsonl").exists()
            if eval_round == 1:
                verdict = Verdict(
                    tenant=tenant.repo,
                    pr=issue_num,
                    head_sha=sha1,
                    run_id=os.environ["FLOW_RUN_ID"],
                    round=1,
                    verdict="changes_requested",
                    findings=[Finding(id="F1", criterion="AC-1", evidence="check_run:1", message="Still overflows")],
                )
                return TurnOutcome(envelope_text=render(verdict), comment_id=201, head_sha=sha1)
            verdict = Verdict(
                tenant=tenant.repo,
                pr=issue_num,
                head_sha=sha2,
                run_id=os.environ["FLOW_RUN_ID"],
                round=2,
                verdict="approved",
                findings=[],
            )
            return TurnOutcome(envelope_text=render(verdict), comment_id=202, head_sha=sha2)

        async def fake_run_arbitrator_turn(**kwargs: Any) -> TurnOutcome:
            worktrees_seen.append(("arbitrator", os.environ.get("WORKSPACE_DIR", "")))
            return TurnOutcome(envelope_text="arbitrated", head_sha=sha2)

        def bridge_turn_complete(
            cp_url: str,
            run_id: str,
            run_cred: str,
            turn_outcome: TurnOutcome,
            issue_id: int,
            instance_id: str,
            label: str = "",
        ) -> dict[str, Any]:
            resp = cp_client.post(
                f"/v1/runs/{run_id}/turn-complete",
                headers={"Authorization": f"Bearer {run_cred}"},
                json={
                    "comment_body": turn_outcome.comment_body or turn_outcome.envelope_text,
                    "comment_id": turn_outcome.comment_id,
                    "head_sha": turn_outcome.head_sha,
                    "issue_number": issue_id,
                    "instance_id": instance_id,
                    "trigger_label": label,
                },
            )
            assert resp.status_code == 200
            return resp.json()

        import pathlib

        fake_gh = MagicMock()
        fake_gh.get_comments.side_effect = lambda repo, num: disp.comments.get((repo, num), [])

        async def _boot_vm(payload: dict[str, Any], instance_id: str) -> AsyncMock:
            vm_env = {
                "FLOW_ROLE": payload["role"],
                "FLOW_RUN_ID": payload["run_id"],
                "FLOW_RUN_CREDENTIAL": payload["run_credential"],
                "FLOW_HEAD_SHA": payload.get("head_sha") or sha1,
                "TARGET_REPO": tenant.repo,
                "TARGET_REF": sha1,
                "ISSUE_ID": str(issue_num),
                "ISSUE_TEXT": well_specified_body,
                "GITHUB_TOKEN": "gh-tok",
                "CONTROL_PANEL_URL": "http://testserver",
                "INSTANCE_ID": instance_id,
                "FLOW_BASE_REPO_DIR": str(base_repo),
                "FLOW_WORKTREES_ROOT": str(wt_root),
            }
            with patch.dict(os.environ, vm_env, clear=True):
                with patch("flow_agent.main.prepare_role_worktree", side_effect=real_worktree_wrapper), \
                     patch("flow_agent.main.RealGitHubClient", return_value=fake_gh), \
                     patch("flow_agent.main.run_developer", side_effect=fake_run_developer), \
                     patch("flow_agent.main.run_evaluator", side_effect=fake_run_evaluator), \
                     patch("flow_agent.main._run_arbitrator_turn", side_effect=fake_run_arbitrator_turn), \
                     patch("flow_agent.main._report_turn_complete", side_effect=bridge_turn_complete), \
                     patch("flow_agent.main._shutdown", new_callable=AsyncMock) as mock_shutdown, \
                     patch("flow_agent.main.SessionRegistry", autospec=True), \
                     patch("flow_agent.main.GitHubTelemetryUI", autospec=True), \
                     patch("flow_agent.main.fetch_run_wiki", return_value={"purpose": "Ollama TUI monitor"}), \
                     patch("argparse.ArgumentParser.parse_known_args", return_value=(MagicMock(role=payload["role"]), [])):
                    await main()
                    return mock_shutdown

        # VM 1: developer R1 -> shuts down (no warm handover into verifying role evaluator) and cold-dispatches evaluator R1
        shutdown1 = await _boot_vm(init_payload, "i-warm-dev1")
        shutdown1.assert_called_once()
        assert len(disp.dispatched) == 1
        _, eval1_payload = disp.dispatched.pop()
        assert eval1_payload["role"] == "evaluator"

        # VM 2: evaluator R1 (changes_requested) -> warm-hands over to developer R2 on the same VM, then cold-dispatches evaluator R2
        shutdown2 = await _boot_vm(eval1_payload, "i-warm-eval1")
        shutdown2.assert_called_once()
        assert len(disp.dispatched) == 1
        _, eval2_payload = disp.dispatched.pop()
        assert eval2_payload["role"] == "evaluator"

        # VM 3: evaluator R2 (approved) -> shuts down (no warm handover into arbitrator); serverless arbitrator runs in-process
        shutdown3 = await _boot_vm(eval2_payload, "i-warm-eval2")
        shutdown3.assert_called_once()
        assert disp.dispatched == []
        assert len(store.arbitration_decisions) == 1
        assert store.arbitration_decisions[0]["head_sha"] == sha2
        assert store.arbitration_decisions[0]["decision"] == "pass"

        roles_executed = [r for r, _ in worktrees_seen]
        assert roles_executed == ["developer", "evaluator", "developer", "evaluator"]
        # Developer R1 and R2 reused the same developer-33 worktree
        assert worktrees_seen[0][1] == worktrees_seen[2][1]
        # Evaluator R1 and R2 used distinct clean worktrees isolated from developer
        assert worktrees_seen[1][1] != worktrees_seen[0][1]
        assert worktrees_seen[3][1] != worktrees_seen[0][1]
        assert shutdown3.call_args[1].get("error") is False
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_e2e_plan_first_trajectory_on_single_warm_supervisor(tmp_path):
    """Plan-first trajectory: underspecified issue -> planner (flow-plan) -> developer
    -> evaluator (approved) -> arbitrator on 1 warm supervisor with zero cold Spot dispatches.
    """
    origin_dir, sha1, _ = _init_git_origin(tmp_path)
    base_repo = tmp_path / "repo-base"
    wt_root = tmp_path / "worktrees"

    store = MemoryStore()
    tenant = store.create_tenant("arsenyspb/ollama-monitor", "arsenyspb")
    store.update_tenant_status(tenant.id, "active")
    store.tenants[tenant.id] = dataclasses.replace(
        store.tenants[tenant.id],
        manifest={"repo": "arsenyspb/ollama-monitor", "required_checks": [], "loop": {"max_review_rounds": 4}},
    )
    store.wikis[tenant.id] = {"purpose": "Ollama TUI monitor"}

    disp = _E2EDispatcher()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_dispatcher] = lambda: disp
    app.dependency_overrides[current_tenant] = lambda: store.tenants[tenant.id]
    cp_client = TestClient(app)

    try:
        issue_num = 43
        vague_body = "Fix the dashboard overflow when resized."
        disp.issue_bodies[(tenant.repo, issue_num)] = vague_body

        # 1. Ingress on POST /v1/events routes underspecified issue to planner
        r_init = cp_client.post(
            "/v1/events",
            json={"kind": "issues.labeled", "issue_number": issue_num, "label": "flow:fix"},
        )
        assert r_init.status_code == 202 and r_init.json()["dispatched"] is True
        assert len(disp.dispatched) == 1
        wf, init_payload = disp.dispatched[0]
        assert wf == "flow_run"
        assert init_payload["role"] == "planner"
        disp.dispatched.clear()

        roles_seen: list[str] = []

        def real_worktree_wrapper(**kwargs: Any):
            kwargs["clone_url"] = str(origin_dir)
            kwargs["setup_cmds"] = []
            ws = prepare_role_worktree(**kwargs)
            roles_seen.append(kwargs["role"])
            return ws

        async def fake_run_planner(**kwargs: Any) -> TurnOutcome:
            plan = Plan(
                tenant=tenant.repo,
                pr=issue_num,
                head_sha="0" * 40,
                run_id=os.environ["FLOW_RUN_ID"],
                complexity="single_pr",
                goal="Clamp dashboard table width",
                scope_files=["app.py"],
                acceptance_criteria=[PlannedCriterion(id="AC-1", description="No overflow")],
                implementation_steps=["Edit app.py"],
            )
            return TurnOutcome(envelope_text=render(plan), comment_id=301, head_sha="0" * 40)

        async def fake_run_developer(**kwargs: Any) -> TurnOutcome:
            assert kwargs.get("latest_plan") is not None
            assert kwargs["latest_plan"].goal == "Clamp dashboard table width"
            ack = Ack(
                tenant=tenant.repo,
                pr=issue_num,
                head_sha=sha1,
                run_id=os.environ["FLOW_RUN_ID"],
            )
            return TurnOutcome(envelope_text=render(ack), comment_id=302, head_sha=sha1)

        async def fake_run_evaluator(**kwargs: Any) -> TurnOutcome:
            assert kwargs.get("latest_plan") is not None
            verdict = Verdict(
                tenant=tenant.repo,
                pr=issue_num,
                head_sha=sha1,
                run_id=os.environ["FLOW_RUN_ID"],
                round=1,
                verdict="approved",
                findings=[],
            )
            return TurnOutcome(envelope_text=render(verdict), comment_id=303, head_sha=sha1)

        async def fake_run_arbitrator_turn(**kwargs: Any) -> TurnOutcome:
            return TurnOutcome(envelope_text="arbitrated", head_sha=sha1)

        def bridge_turn_complete(
            cp_url: str,
            run_id: str,
            run_cred: str,
            turn_outcome: TurnOutcome,
            issue_id: int,
            instance_id: str,
            label: str = "",
        ) -> dict[str, Any]:
            resp = cp_client.post(
                f"/v1/runs/{run_id}/turn-complete",
                headers={"Authorization": f"Bearer {run_cred}"},
                json={
                    "comment_body": turn_outcome.comment_body or turn_outcome.envelope_text,
                    "comment_id": turn_outcome.comment_id,
                    "head_sha": turn_outcome.head_sha,
                    "issue_number": issue_id,
                    "instance_id": instance_id,
                    "trigger_label": label,
                },
            )
            assert resp.status_code == 200
            return resp.json()

        env_vars = {
            "FLOW_ROLE": init_payload["role"],
            "FLOW_RUN_ID": init_payload["run_id"],
            "FLOW_RUN_CREDENTIAL": init_payload["run_credential"],
            "FLOW_HEAD_SHA": "0" * 40,
            "TARGET_REPO": tenant.repo,
            "TARGET_REF": sha1,
            "ISSUE_ID": str(issue_num),
            "ISSUE_TEXT": vague_body,
            "GITHUB_TOKEN": "gh-tok",
            "CONTROL_PANEL_URL": "http://testserver",
            "INSTANCE_ID": "i-warm-planfirst",
            "FLOW_BASE_REPO_DIR": str(base_repo),
            "FLOW_WORKTREES_ROOT": str(wt_root),
        }

        fake_gh = MagicMock()
        fake_gh.get_comments.side_effect = lambda repo, num: disp.comments.get((repo, num), [])

        with patch.dict(os.environ, env_vars, clear=True):
            with patch("flow_agent.main.prepare_role_worktree", side_effect=real_worktree_wrapper), \
                 patch("flow_agent.main.RealGitHubClient", return_value=fake_gh), \
                 patch("flow_agent.main.run_planner", side_effect=fake_run_planner), \
                 patch("flow_agent.main.run_developer", side_effect=fake_run_developer), \
                 patch("flow_agent.main.run_evaluator", side_effect=fake_run_evaluator), \
                 patch("flow_agent.main._run_arbitrator_turn", side_effect=fake_run_arbitrator_turn), \
                 patch("flow_agent.main._report_turn_complete", side_effect=bridge_turn_complete), \
                 patch("flow_agent.main._shutdown", new_callable=AsyncMock) as mock_shutdown, \
                 patch("flow_agent.main.SessionRegistry", autospec=True), \
                 patch("flow_agent.main.GitHubTelemetryUI", autospec=True), \
                 patch("flow_agent.main.fetch_run_wiki", return_value={"purpose": "Ollama TUI monitor"}), \
                 patch("argparse.ArgumentParser.parse_known_args", return_value=(MagicMock(role="planner"), [])):
                await main()

        # Planner -> Developer runs on the same warm supervisor; Evaluator is cold-dispatched (J43 WP-2)
        assert roles_seen == ["planner", "developer"]
        assert len(disp.dispatched) == 1
        assert disp.dispatched[0][1]["role"] == "evaluator"
        mock_shutdown.assert_called_once()
        assert mock_shutdown.call_args[1].get("error") is False
    finally:
        app.dependency_overrides.clear()
