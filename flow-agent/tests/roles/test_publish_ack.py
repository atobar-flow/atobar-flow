from unittest.mock import MagicMock, patch

from atobar_contracts.github_rest import RealGitHubClient
from atobar_contracts.envelopes import Ack, render
from flow_agent.roles.developer.runner import _publish_ack


def test_publish_ack_posts_when_prior_ack_has_different_run_id(monkeypatch):
    monkeypatch.setenv("FLOW_RUN_ID", "run-round-2")

    prior_ack = Ack(
        tenant="owner/repo",
        pr=42,
        head_sha="1111111111111111111111111111111111111111",
        run_id="run-round-1",
        addressed_findings=[],
        dispute=[],
    )
    prior_comment = {"body": render(prior_ack)}

    gh_client = MagicMock(spec=RealGitHubClient)
    gh_client.get_comments.return_value = [prior_comment]

    current_ack = Ack(
        tenant="owner/repo",
        pr=42,
        head_sha="2222222222222222222222222222222222222222",
        run_id="run-round-2",
        addressed_findings=[],
        dispute=[],
    )

    with patch("flow_agent.roles.developer.runner.create_and_verify_pr"):
        with patch("flow_agent.roles.developer.runner.get_current_git_sha", return_value="2222222222222222222222222222222222222222"):
            _publish_ack("owner/repo", 42, render(current_ack), gh_client)

    gh_client.post_pr_comment.assert_called_once()
    assert "run-round-2" in gh_client.post_pr_comment.call_args[1]["body"]


def test_publish_ack_dedups_when_prior_ack_has_same_run_id(monkeypatch):
    monkeypatch.setenv("FLOW_RUN_ID", "run-round-1")

    prior_ack = Ack(
        tenant="owner/repo",
        pr=42,
        head_sha="1111111111111111111111111111111111111111",
        run_id="run-round-1",
        addressed_findings=[],
        dispute=[],
    )
    prior_comment = {"body": render(prior_ack)}

    gh_client = MagicMock(spec=RealGitHubClient)
    gh_client.get_comments.return_value = [prior_comment]

    current_ack = Ack(
        tenant="owner/repo",
        pr=42,
        head_sha="1111111111111111111111111111111111111111",
        run_id="run-round-1",
        addressed_findings=[],
        dispute=[],
    )

    with patch("flow_agent.roles.developer.runner.create_and_verify_pr"):
        with patch("flow_agent.roles.developer.runner.get_current_git_sha", return_value="1111111111111111111111111111111111111111"):
            _publish_ack("owner/repo", 42, render(current_ack), gh_client)

    gh_client.post_pr_comment.assert_not_called()


def test_publish_ack_posts_when_same_sha_has_different_run_id(monkeypatch):
    """Devin #1: A restarted developer run pushing the same SHA must post a new ack with its own run_id."""
    monkeypatch.setenv("FLOW_RUN_ID", "run-round-2")

    prior_ack = Ack(
        tenant="owner/repo",
        pr=42,
        head_sha="1111111111111111111111111111111111111111",
        run_id="run-round-1",
        addressed_findings=[],
        dispute=[],
    )
    gh_client = MagicMock(spec=RealGitHubClient)
    gh_client.get_comments.return_value = [{"id": 10, "body": render(prior_ack)}]
    gh_client.post_pr_comment.return_value = {"id": 20}

    current_ack = Ack(
        tenant="owner/repo",
        pr=42,
        head_sha="1111111111111111111111111111111111111111",
        run_id="run-round-2",
        addressed_findings=[],
        dispute=[],
    )

    with patch(
        "flow_agent.roles.developer.runner.create_and_verify_pr",
        return_value="PR Creation Output: https://github.com/owner/repo/pull/42",
    ):
        with patch(
            "flow_agent.roles.developer.runner.get_current_git_sha",
            return_value="1111111111111111111111111111111111111111",
        ):
            body, cid, _ = _publish_ack("owner/repo", 42, render(current_ack), gh_client)

    gh_client.post_pr_comment.assert_called_once()
    assert cid == 20
    assert "run-round-2" in body


def test_j42_wp2_workflow_path_guard_drops_workflows_and_explains_handoff(tmp_path, monkeypatch):
    """J42 WP-2: developer changes under .github/workflows/** are dropped before push and reported in PR/ack."""
    import subprocess

    from flow_agent.tools.git_tools import create_and_verify_pr

    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "--bare"], cwd=origin, check=True, stdout=subprocess.DEVNULL)

    ws = tmp_path / "ws"
    subprocess.run(["git", "clone", str(origin), str(ws)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=ws, check=True)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=ws, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (ws / "app.py").write_text("v1\n")
    wf_dir = ws / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "ci.yml").write_text("name: CI\n")
    subprocess.run(["git", "add", "."], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=ws, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=ws, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    monkeypatch.setenv("WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("TARGET_REPO", "owner/repo")
    monkeypatch.setenv("ISSUE_ID", "42")
    monkeypatch.setenv("FLOW_RUN_ID", "run-wf-1")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")

    # Case 1: Only .github/workflows/ci.yml is modified -> nothing pushed, explanatory comment posted without flow-ack
    (wf_dir / "ci.yml").write_text("name: Modified CI\n")
    gh_client = MagicMock(spec=RealGitHubClient)
    gh_client.get_comments.return_value = []
    gh_client.post_pr_comment.return_value = {"id": 301}
    ack = Ack(
        tenant="owner/repo",
        pr=42,
        head_sha="1" * 40,
        run_id="run-wf-1",
        addressed_findings=[],
        dispute=[],
    )
    body1, cid1, _ = _publish_ack("owner/repo", 42, render(ack), gh_client)
    assert cid1 == 301
    assert body1 == ""
    posted_note = gh_client.post_pr_comment.call_args.kwargs["body"]
    assert "Workflow file changes need a human: .github/workflows/ci.yml" in posted_note
    assert "```flow-ack" not in posted_note

    # Case 2: Both app.py and .github/workflows/ci.yml are modified + screenshot recorded ->
    # workflow file dropped, app.py pushed, and both workflow_note and screenshot appear in the ack comment
    (ws / "app.py").write_text("v2\n")
    (wf_dir / "ci.yml").write_text("name: Modified CI 2\n")
    captured_pr_bodies: list[str] = []

    def fake_create_pr_api(owner, repo, title, body, base, head, token):
        captured_pr_bodies.append(body)
        return True, "https://github.com/owner/repo/pull/99"

    gh_client.post_pr_comment.reset_mock()
    gh_client.post_pr_comment.return_value = {"id": 302}
    with patch("flow_agent.tools.git_tools._create_pr_api", side_effect=fake_create_pr_api):
        with patch(
            "flow_agent.roles.developer.runner.load_recorded_screenshots",
            return_value=[{"url": "https://img.example/s.png", "markdown": "![UI](https://img.example/s.png)"}],
        ):
            body2, cid2, _ = _publish_ack("owner/repo", 42, render(ack), gh_client)
    assert cid2 == 302
    assert "Workflow file changes need a human: .github/workflows/ci.yml" in body2
    assert "### 📸 Developer UI Screenshots" in body2
    assert "```flow-ack" in body2
    assert len(captured_pr_bodies) == 1
    assert "Workflow file changes need a human: .github/workflows/ci.yml" in captured_pr_bodies[0]
    diff_pushed = subprocess.check_output(
        ["git", "diff", "--name-only", "origin/main..HEAD"], cwd=ws, text=True
    ).splitlines()
    assert diff_pushed == ["app.py"]


def test_j42_wp4_sandbox_setup_commands_reject_unsafe_commands(tmp_path):
    from flow_agent.runtime.sandbox import _run_setup_commands

    failure = _run_setup_commands(
        repo="owner/repo",
        ref="main",
        target_path=tmp_path,
        marker_dir=tmp_path,
        exact_sha="a" * 40,
        setup_cmds=["echo 1\ninjected"],
        timeout_s=5.0,
    )
    assert failure is not None
    assert failure.returncode == 1
    assert "newline" in failure.output


def test_update_existing_pr_body_deduplicates_already_embedded_screenshots():
    """#357: Fallback _publish_ack PR update must not duplicate screenshots already embedded in the PR body."""
    from flow_agent.tools.git_tools import _update_existing_pr_body

    url1 = "https://cp.example.invalid/v1/runs/run_79380f3368d6c33d/screenshots/d4f5205c2ced202a.png?exp=1793358024&sig=" + ("a" * 64)
    url2 = "https://cp.example.invalid/v1/runs/run_79380f3368d6c33d/screenshots/second_capture.png?exp=1793358024&sig=" + ("b" * 64)

    existing_body = (
        "Fixes #53\n\n## What\n- Doubled TUI chart density\n\n"
        f"![UI Inspection Screenshot]({url1})"
    )
    fallback_body_same = (
        "Fixes #53\n\nAutomated implementation by atobar-flow developer (run run_79380f3368d6c33d).\n\nCloses #53\n\n"
        f"## Screenshots\n\n![Screenshot]({url1})"
    )

    fake_client = MagicMock()
    fake_get_resp = MagicMock()
    fake_get_resp.status_code = 200
    fake_get_resp.json.return_value = [{"number": 54, "body": existing_body}]
    fake_client.get.return_value = fake_get_resp

    # 1. When all screenshots in fallback_body are already in existing_body, do not patch duplicate ## Screenshots
    _update_existing_pr_body(fake_client, "arsenyspb", "ollama-monitor", "flow/issue-53", fallback_body_same)
    fake_client.patch.assert_not_called()

    # 2. When fallback_body has one already-present screenshot (url1) and one new screenshot (url2), only append url2
    fallback_body_mixed = (
        "Fixes #53\n\nAutomated implementation by atobar-flow developer (run run_79380f3368d6c33d).\n\nCloses #53\n\n"
        f"## Screenshots\n\n![Screenshot]({url1})\n\n![Screenshot]({url2})"
    )
    _update_existing_pr_body(fake_client, "arsenyspb", "ollama-monitor", "flow/issue-53", fallback_body_mixed)
    fake_client.patch.assert_called_once()
    patched_body = fake_client.patch.call_args.kwargs["json"]["body"]
    assert patched_body.count("d4f5205c2ced202a.png") == 1
    assert patched_body.count("second_capture.png") == 1


def test_role_comments_hide_raw_json_envelope_in_html_comment(tmp_path, monkeypatch):
    """#361: Developer, Evaluator, Planner, and Arbitrator comments hide ```flow-* JSON inside <!-- atobar-envelope -->."""
    import re
    from atobar_contracts.envelopes import Plan, PlannedCriterion, Verdict, render
    from flow_agent.roles.evaluator.spec import post_evaluator_verdict
    from flow_agent.roles.planner.runner import _publish_plan

    # 1. Planner comment includes human-readable plan summary and hides ```flow-plan inside <!-- ... -->
    plan = Plan(
        tenant="owner/repo",
        pr=53,
        head_sha="0" * 40,
        run_id="run-plan-1",
        complexity="single_pr",
        goal="Double TUI chart density",
        scope_files=["src/monitor_tui.py"],
        acceptance_criteria=[PlannedCriterion(id="AC-1", description="2x braille density")],
        implementation_steps=["Update src/monitor_tui.py"],
    )
    gh_plan = MagicMock()
    gh_plan.post_pr_comment.return_value = {"id": 11}
    _publish_plan("owner/repo", 53, "0" * 40, render(plan), gh_plan, manifest={})
    plan_comment = gh_plan.post_pr_comment.call_args.kwargs["body"]
    visible_plan = re.sub(r"<!--.*?-->", "", plan_comment, flags=re.DOTALL).strip()
    assert "Double TUI chart density" in visible_plan
    assert "```flow-plan" not in visible_plan
    assert "<!-- atobar-envelope" in plan_comment

    # 2. Evaluator comment hides ```flow-verdict inside <!-- ... -->
    verdict = Verdict(
        tenant="owner/repo",
        pr=53,
        head_sha="a" * 40,
        run_id="run-eval-1",
        round=1,
        verdict="approved",
        findings=[],
    )
    gh_eval = MagicMock()
    gh_eval.post_pr_comment.return_value = {"id": 12}
    post_evaluator_verdict(gh_eval, "owner/repo", 53, verdict, "a" * 40)
    eval_comment = gh_eval.post_pr_comment.call_args.kwargs["body"]
    visible_eval = re.sub(r"<!--.*?-->", "", eval_comment, flags=re.DOTALL).strip()
    assert "ATOBAR Evaluator Review" in visible_eval
    assert "```flow-verdict" not in visible_eval
    assert "<!-- atobar-envelope" in eval_comment

    # 3. Developer comment preserves headline with and without screenshots and hides ```flow-ack inside <!-- ... -->
    monkeypatch.setenv("FLOW_RUN_ID", "run-dev-1")
    dev_sha = "b" * 40
    dev_ack = Ack(tenant="owner/repo", pr=53, head_sha=dev_sha, run_id="run-dev-1")
    gh_dev = MagicMock(spec=RealGitHubClient)
    gh_dev.get_comments.return_value = []
    gh_dev.post_pr_comment.return_value = {"id": 13}
    with (
        patch("flow_agent.roles.developer.runner.create_and_verify_pr", return_value="PR Creation Output: https://github.com/owner/repo/pull/53"),
        patch("flow_agent.roles.developer.runner.get_current_git_sha", return_value=dev_sha),
        patch("flow_agent.roles.developer.runner.load_recorded_screenshots", return_value=[{"url": "https://img.example/dev.png"}]),
    ):
        dev_comment, _, _ = _publish_ack("owner/repo", 53, render(dev_ack), gh_dev)
    visible_dev = re.sub(r"<!--.*?-->", "", dev_comment, flags=re.DOTALL).strip()
    assert f"✅ **Developer** pushed `{dev_sha[:7]}` ready for evaluation." in visible_dev
    assert "### 📸 Developer UI Screenshots" in visible_dev
    assert "```flow-ack" not in visible_dev
    assert "<!-- atobar-envelope" in dev_comment

    # 4. Arbitrator comment hides ```flow-arbitration inside <!-- ... -->
    import asyncio
    from atobar_contracts.arbitration import run_arbitrator
    from atobar_contracts.manifest import TenantManifest

    manifest_obj = TenantManifest.model_validate({
        "repo": "owner/repo",
        "owner_github_login": "owner",
        "arbitration": {"mode": "recommend"},
        "metrics": [{"key": "m1", "comparator": ">=", "threshold": 1.0}],
    })
    gh_arb = MagicMock()
    asyncio.run(
        run_arbitrator(
            manifest=manifest_obj,
            pr=53,
            head_sha=dev_sha,
            pr_observations=[],
            base_observations=[],
            changed_paths=[],
            gh=gh_arb,
        )
    )
    arb_comment = gh_arb.post_pr_comment.call_args.kwargs["body"]
    visible_arb = re.sub(r"<!--.*?-->", "", arb_comment, flags=re.DOTALL).strip()
    assert "flow arbitrator" in visible_arb
    assert "```flow-arbitration" not in visible_arb
    assert "<!-- atobar-envelope" in arb_comment


