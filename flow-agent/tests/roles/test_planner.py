import hashlib
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from control_panel.app import app
from control_panel.llm_keys import encrypt, get_kek
from control_panel.store import MemoryStore, get_store
from flow_agent.agents.base import AgentRuntime, RoleResult, RoleTask, RuntimeHooks, UsageLedger
from flow_agent.agents.registry import register_runtime
from flow_agent.llm.protocol import Reply
from atobar_contracts.envelopes import Plan, PlannedCriterion, ProposedIssueDraft, render
from flow_agent.roles.base import TurnOutcome
from flow_agent.roles.planner.runner import run_planner
from flow_agent.roles.planner.spec import (
    build_planner_context,
    get_planner_spec,
    planner_done_predicate,
)
from flow_agent.runtime.key_delivery import TenantLLMKey
from flow_agent.runtime.tools import allowlist_for

SENTINEL_SHA = "0" * 40
KEK = b"0123456789abcdef0123456789abcdef"


def test_ac1_planner_allowlist_is_strictly_read_only():
    tools = allowlist_for("planner")
    assert tools == frozenset({
        "read_file",
        "search_codebase",
        "git_pr_checks",
        "git_ci_job_logs",
        "todo_write",
    })
    assert "execute_command" not in tools
    assert "write_file" not in tools
    assert "multi_edit" not in tools
    assert "create_and_verify_pr" not in tools


def test_planner_done_predicate_and_context():
    plan = Plan(
        tenant="arsenyspb/ollama-monitor",
        pr=33,
        head_sha=SENTINEL_SHA,
        complexity="single_pr",
        goal="Fix overflow",
        scope_files=["src/ui.py"],
        acceptance_criteria=[PlannedCriterion(id="AC-1", description="Fits 375px")],
        implementation_steps=["Edit src/ui.py"],
    )
    assert planner_done_predicate(Reply(text=f"Done:\n{render(plan)}")) is True
    assert planner_done_predicate(Reply(text="Still thinking...")) is False

    ctx = build_planner_context(
        tenant="arsenyspb/ollama-monitor",
        pr=33,
        head_sha=SENTINEL_SHA,
        wiki={
            "purpose": "TUI monitor for Ollama",
            "technologies": ["Rust", "Ratatui"],
            "structure": {"src/main.rs": "entrypoint"},
            "concepts": ["polling loop"],
        },
        manifest={"default_branch": "main", "required_checks": ["ci/test"]},
    )
    assert "TUI monitor for Ollama" in ctx
    assert "Ratatui" in ctx
    assert "src/main.rs" in ctx
    assert "polling loop" in ctx
    assert "ci/test" in ctx

    from flow_agent.llm.fake import FakeLLMClient

    spec = get_planner_spec(FakeLLMClient([]), model_name="gemini-3.8-flash")
    assert spec.name == "planner"
    assert spec.tools == allowlist_for("planner")


class _FakePlannerRuntime(AgentRuntime):
    def __init__(self, plan_envelope: str):
        self.plan_envelope = plan_envelope
        self.recorded_task: RoleTask | None = None

    async def run(self, task: RoleTask, *, hooks: RuntimeHooks) -> RoleResult:
        self.recorded_task = task
        return RoleResult(
            final_text=self.plan_envelope,
            envelope_text=self.plan_envelope,
            usage=UsageLedger(),
            halt="done",
        )


@pytest.mark.asyncio
async def test_ac2_run_planner_fetches_wiki_validates_and_posts_plan():
    plan = Plan(
        tenant="arsenyspb/ollama-monitor",
        pr=33,
        head_sha=SENTINEL_SHA,
        complexity="single_pr",
        goal="Localize and fix TUI layout overflow",
        scope_files=["src/ui/dashboard.rs", "tests/ui_test.rs"],
        out_of_scope=["Network layer"],
        acceptance_criteria=[
            PlannedCriterion(
                id="AC-1",
                description="No overflow on 80x24 terminal",
                verification_cmd="cargo test",
            )
        ],
        implementation_steps=["Update layout constraints in src/ui/dashboard.rs"],
    )
    fake_rt = _FakePlannerRuntime(render(plan))
    register_runtime("fake_planner_rt", lambda: fake_rt)

    gh_client = MagicMock()
    gh_client.post_pr_comment.return_value = {"id": 987654}

    env = {
        "CONTROL_PANEL_URL": "http://fake-cp",
        "FLOW_RUN_ID": "run_plan_99",
        "FLOW_RUN_CREDENTIAL": "cred_plan_99",
        "FLOW_AGENT_RUNTIME": "fake_planner_rt",
    }
    with patch.dict(os.environ, env, clear=True):
        with patch("flow_agent.roles.planner.runner.fetch_run_llm_key") as mock_key:
            mock_key.return_value = TenantLLMKey(
                provider="gemini",
                api_key="k-123",
                model="gemini-3.8-flash",
            )

            outcome = await run_planner(
                tenant="arsenyspb/ollama-monitor",
                pr=33,
                task_prompt="Fix the dashboard overflow",
                head_sha=SENTINEL_SHA,
                wiki={
                    "purpose": "Monitor local Ollama models",
                    "technologies": ["Python"],
                    "structure": "src/",
                },
                gh_client=gh_client,
            )

    assert isinstance(outcome, TurnOutcome)
    assert outcome.comment_id == 987654
    assert outcome.head_sha == SENTINEL_SHA
    assert "```flow-plan" in outcome.envelope_text
    assert "run_plan_99" in outcome.envelope_text
    assert fake_rt.recorded_task is not None
    assert fake_rt.recorded_task.role == "planner"
    assert fake_rt.recorded_task.done_envelope == "flow-plan"
    assert "Monitor local Ollama models" in fake_rt.recorded_task.context
    gh_client.post_pr_comment.assert_called_once()


def test_ac4_control_panel_run_wiki_and_planner_llm_fallback():
    store = MemoryStore()
    tenant = store.create_tenant("arsenyspb/ollama-monitor", "arsenyspb")
    store.put_llm_key(
        tenant.id,
        "gemini",
        {"provider": "gemini", "ciphertext": encrypt(KEK, tenant.id, "gemini", "k-secret")},
    )
    # Only configure developer model (no explicit planner model)
    store.set_role_llm_pref(tenant.id, "developer", "gemini", "gemini-3.8-flash", "medium")
    store.wikis[tenant.id] = {
        "purpose": "Ollama monitoring dashboard",
        "technologies": ["Python", "FastAPI"],
        "structure": {"src/": "main code"},
        "concepts": ["metrics"],
    }

    cred = "atb_run_plan_cred"
    run_id = "run_planner_1"
    store.run_creds[run_id] = {
        "run_id": run_id,
        "tenant_id": tenant.id,
        "role": "planner",
        "cred_hash": hashlib.sha256(cred.encode()).hexdigest(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    }

    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_kek] = lambda: KEK
    client = TestClient(app)
    try:
        # 1. Planner LLM key falls back to developer model config when planner not separately configured
        r_key = client.post(
            f"/v1/runs/{run_id}/llm-key",
            headers={"Authorization": f"Bearer {cred}"},
        )
        assert r_key.status_code == 200
        data = r_key.json()
        assert data["provider"] == "gemini"
        assert data["model"] == "gemini-3.8-flash"
        assert data["reasoning_effort"] == "medium"

        # 2. GET /v1/runs/{run_id}/wiki returns the tenant's SotaWiki authenticated by run credential
        r_wiki = client.get(
            f"/v1/runs/{run_id}/wiki",
            headers={"Authorization": f"Bearer {cred}"},
        )
        assert r_wiki.status_code == 200
        assert r_wiki.json()["purpose"] == "Ollama monitoring dashboard"

        # Invalid credential rejected
        r_bad = client.get(
            f"/v1/runs/{run_id}/wiki",
            headers={"Authorization": "Bearer wrong"},
        )
        assert r_bad.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_ac4_role_specs_require_corner_case_and_assumption_audit():
    from flow_agent.llm.fake import FakeLLMClient
    from flow_agent.roles.developer.spec import get_developer_spec
    from flow_agent.roles.evaluator.spec import get_evaluator_spec

    llm = FakeLLMClient([])
    planner_spec = get_planner_spec(llm)
    dev_spec = get_developer_spec(llm)
    eval_spec = get_evaluator_spec(llm)

    assert "Corner-Case" in planner_spec.system_prompt or "corner cases" in planner_spec.system_prompt.lower()
    assert "assumption" in planner_spec.system_prompt.lower()
    assert ".github/ISSUE_TEMPLATE/flow-task.md" in planner_spec.system_prompt

    assert "Corner-Case" in dev_spec.system_prompt
    assert "Assumption" in dev_spec.system_prompt
    assert ".github/PULL_REQUEST_TEMPLATE.md" in dev_spec.system_prompt

    assert "Corner-Case" in eval_spec.system_prompt
    assert "Assumption" in eval_spec.system_prompt
    assert ".github/REVIEW_TEMPLATE.md" in eval_spec.system_prompt


@pytest.mark.asyncio
async def test_ac3_goal_planner_rejects_invalid_drafts_before_github_call():
    import json

    manifest = {
        "repo": "arsenyspb/ollama-monitor",
        "goal": {
            "objective": "Raise coverage_pct above 85%",
            "targets": ["coverage_pct"],
            "guardrails": ["test_pass_rate"],
            "autonomy": "approve",
            "tracking_issue_number": 10,
        },
    }
    valid_draft = {
        "title": "Add unit tests for parser",
        "goal": "Cover edge cases in parser.py",
        "scope_files": ["src/parser.py", "tests/test_parser.py"],
        "acceptance_criteria": [{"id": "AC-1", "description": "Parser coverage >= 90%", "verification_cmd": "pytest"}],
        "target_metric_key": "coverage_pct",
        "rationale": "Moves coverage_pct toward 85% target",
    }

    # Case 1: >3 proposed issues
    too_many = {
        "v": 1,
        "tenant": "arsenyspb/ollama-monitor",
        "pr": 10,
        "head_sha": SENTINEL_SHA,
        "role": "planner",
        "complexity": "multi_issue_epic",
        "goal": "Propose coverage tasks",
        "proposed_issues": [valid_draft, valid_draft, valid_draft, valid_draft],
    }
    # Case 2: missing acceptance_criteria
    no_ac = {
        "v": 1,
        "tenant": "arsenyspb/ollama-monitor",
        "pr": 10,
        "head_sha": SENTINEL_SHA,
        "role": "planner",
        "complexity": "multi_issue_epic",
        "goal": "Propose coverage tasks",
        "proposed_issues": [{**valid_draft, "acceptance_criteria": []}],
    }
    # Case 3: unknown target_metric_key
    bad_metric = {
        "v": 1,
        "tenant": "arsenyspb/ollama-monitor",
        "pr": 10,
        "head_sha": SENTINEL_SHA,
        "role": "planner",
        "complexity": "multi_issue_epic",
        "goal": "Propose coverage tasks",
        "proposed_issues": [{**valid_draft, "target_metric_key": "nonexistent_metric"}],
    }

    for idx, payload in enumerate((too_many, no_ac, bad_metric)):
        raw_env = f"```flow-plan\n{json.dumps(payload)}\n```\n"
        rt_name = f"fake_bad_goal_rt_{idx}"
        register_runtime(rt_name, lambda env_str=raw_env: _FakePlannerRuntime(env_str))
        gh_client = MagicMock()
        env = {
            "CONTROL_PANEL_URL": "http://fake-cp",
            "FLOW_RUN_ID": f"run_bad_goal_{idx}",
            "FLOW_RUN_CREDENTIAL": "cred_bad_goal",
            "FLOW_AGENT_RUNTIME": rt_name,
        }
        with patch.dict(os.environ, env, clear=True):
            with patch("flow_agent.roles.planner.runner.fetch_run_llm_key") as mock_key:
                mock_key.return_value = TenantLLMKey(provider="gemini", api_key="k", model="gemini-3.8-flash")
                with pytest.raises(ValueError):
                    await run_planner(
                        tenant="arsenyspb/ollama-monitor",
                        pr=10,
                        task_prompt="Propose next issues",
                        head_sha=SENTINEL_SHA,
                        manifest=manifest,
                        gh_client=gh_client,
                    )
        gh_client.post_pr_comment.assert_not_called()
        gh_client.create_issue.assert_not_called()


@pytest.mark.parametrize(
    "autonomy,expected_label",
    [("approve", "flow:needs-approval"), ("auto", "flow:feature")],
)
def test_ac3_goal_planner_turn_complete_opens_issues_with_autonomy_label(autonomy: str, expected_label: str):
    from control_panel.app import get_dispatcher

    class _FakeGoalDispatcher:
        def __init__(self) -> None:
            self.issues: list[dict[str, object]] = []
            self.comments: list[tuple[str, int, str]] = []

        def create_issue(
            self, repo: str, title: str, body: str, labels: list[str] | None = None
        ) -> dict[str, object]:
            num = 100 + len(self.issues) + 1
            obj: dict[str, object] = {
                "number": num,
                "repo": repo,
                "title": title,
                "body": body,
                "state": "open",
                "labels": [{"name": lbl} for lbl in (labels or [])],
            }
            self.issues.append(obj)
            return obj

        def list_open_issues(self, repo: str) -> list[dict[str, object]]:
            return [i for i in self.issues if i.get("repo") == repo]

        def create_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
            self.comments.append((repo, issue_number, body))

    store = MemoryStore()
    fake_disp = _FakeGoalDispatcher()
    tenant = store.create_tenant("arsenyspb/ollama-monitor", "arsenyspb")
    store.upsert_metric_definition(
        tenant.id,
        {"key": "coverage_pct", "status": "active", "comparator": ">=", "threshold": 85.0},
    )
    store.upsert_metric_definition(
        tenant.id,
        {"key": "test_pass_rate", "status": "active", "comparator": ">=", "threshold": 1.0},
    )
    store.set_tenant_goal(
        tenant.id,
        {
            "objective": "Increase coverage_pct to 85%",
            "targets": ["coverage_pct"],
            "guardrails": ["test_pass_rate"],
            "autonomy": autonomy,
            "tracking_issue_number": 10,
        },
    )

    cred = f"cred_goal_{autonomy}"
    run_id = f"run_goal_{autonomy}"
    store.create_run_credential(
        run_id,
        tenant.id,
        "planner",
        hashlib.sha256(cred.encode()).hexdigest(),
        (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        goal_mode=True,
        plan_count=2,
    )

    plan = Plan(
        tenant="arsenyspb/ollama-monitor",
        pr=10,
        head_sha=SENTINEL_SHA,
        run_id=run_id,
        complexity="multi_issue_epic",
        goal="Propose 2 targeted coverage improvements",
        proposed_issues=[
            ProposedIssueDraft(
                title="Add unit tests for CLI args",
                goal="Cover argument parsing in cli.py",
                scope_files=["src/cli.py", "tests/test_cli.py"],
                acceptance_criteria=[
                    PlannedCriterion(id="AC-1", description="cli.py coverage >= 90%", verification_cmd="pytest")
                ],
                target_metric_key="coverage_pct",
                rationale="cli.py currently has 40% coverage",
            ),
            ProposedIssueDraft(
                title="Add unit tests for formatter",
                goal="Cover table formatting edge cases",
                scope_files=["src/fmt.py", "tests/test_fmt.py"],
                acceptance_criteria=[
                    PlannedCriterion(id="AC-1", description="fmt.py coverage >= 85%", verification_cmd="pytest")
                ],
                target_metric_key="coverage_pct",
                rationale="fmt.py is the second largest uncovered module",
            ),
        ],
    )

    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_dispatcher] = lambda: fake_disp
    client = TestClient(app)
    try:
        resp = client.post(
            f"/v1/runs/{run_id}/turn-complete",
            headers={"Authorization": f"Bearer {cred}"},
            json={
                "comment_body": render(plan),
                "issue_number": 10,
                "head_sha": SENTINEL_SHA,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "terminate"
        assert body["reason"] == "goal_planned"
        assert len(body["created_issues"]) == 2
        assert len(fake_disp.issues) == 2
        for issue in fake_disp.issues:
            labels_list = issue["labels"]
            assert isinstance(labels_list, list)
            assert [lbl["name"] for lbl in labels_list if isinstance(lbl, dict)] == [expected_label, "flow:goal"]
            assert "coverage_pct" in str(issue["body"])
            assert "<!-- atobar:goal_sha:" in str(issue["body"])
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_goal_mode_env_in_run_planner_requires_proposed_issues():
    import base64

    plan_without_proposals = Plan(
        tenant="arsenyspb/ollama-monitor",
        pr=10,
        head_sha=SENTINEL_SHA,
        complexity="single_pr",
        goal="Normal single PR plan without proposed_issues",
        acceptance_criteria=[PlannedCriterion(id="AC-1", description="Pass")],
    )
    fake_rt = _FakePlannerRuntime(render(plan_without_proposals))
    register_runtime("fake_goal_no_proposals_rt", lambda: fake_rt)

    env = {
        "CONTROL_PANEL_URL": "http://fake-cp",
        "FLOW_RUN_ID": "run_goal_env_1",
        "FLOW_RUN_CREDENTIAL": "cred_goal_env_1",
        "FLOW_AGENT_RUNTIME": "fake_goal_no_proposals_rt",
        "FLOW_GOAL_MODE": "1",
        "FLOW_PLAN_COUNT": "2",
        "FLOW_GOAL_OBJECTIVE_B64": base64.b64encode(b"Raise coverage_pct above 85%").decode("ascii"),
    }
    with patch.dict(os.environ, env, clear=True):
        with patch("flow_agent.roles.planner.runner.fetch_run_llm_key") as mock_key:
            mock_key.return_value = TenantLLMKey(provider="gemini", api_key="k", model="gemini-3.8-flash")
            with pytest.raises(ValueError, match="between 1 and 3 issues"):
                await run_planner(
                    tenant="arsenyspb/ollama-monitor",
                    pr=10,
                    task_prompt="Ignored tracking issue body",
                    head_sha=SENTINEL_SHA,
                    manifest={"goal": {"objective": "Raise coverage_pct above 85%", "targets": ["coverage_pct"]}},
                )
    assert fake_rt.recorded_task is not None
    assert "OUTER-LOOP GOAL PLANNING MODE" in fake_rt.recorded_task.task_prompt
    assert "Raise coverage_pct above 85%" in fake_rt.recorded_task.task_prompt


