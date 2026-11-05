"""Unit tests for Evaluator 'Your Next Action' PR UX and dual-thread Issue+PR human comments (J49 #365 WP-3)."""

from typing import Any, Optional

from atobar_contracts.envelopes import Finding, Verdict
from flow_agent.main import _human_comments_for
from flow_agent.roles.developer.spec import build_developer_context
from flow_agent.roles.evaluator.spec import (
    build_evaluator_context,
    format_evaluator_next_action,
    post_evaluator_verdict,
)
from flow_agent.roles.planner.spec import build_planner_context

HEAD_SHA = "9261fc09261fc09261fc09261fc09261fc09261f"


class FakeGitHubForEvalUX:
    def __init__(self, open_pr_num: Optional[int] = 54) -> None:
        self.open_pr_num = open_pr_num
        self.posted_comments: list[dict[str, Any]] = []
        self.check_runs: list[dict[str, Any]] = []
        self.comments: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.reviews: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.review_comments: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.open_prs: list[dict[str, Any]] = []

    def find_pr_for_sha(self, repo: str, sha: str) -> Optional[int]:
        if sha == HEAD_SHA and self.open_pr_num is not None:
            return self.open_pr_num
        return None

    def list_open_prs(self, repo: str) -> list[dict[str, Any]]:
        return list(self.open_prs)

    def post_pr_comment(self, repo: str, pr_number: int, body: str) -> dict[str, Any]:
        cid = 5000 + len(self.posted_comments) + 1
        rec = {"id": cid, "repo": repo, "pr_number": pr_number, "body": body}
        self.posted_comments.append(rec)
        return rec

    def create_check_run(self, repo: str, name: str, head_sha: str, conclusion: str, output: dict[str, str]) -> None:
        self.check_runs.append(
            {"repo": repo, "name": name, "head_sha": head_sha, "conclusion": conclusion, "output": output}
        )

    def get_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        return list(self.comments.get((repo, pr_number), []))

    def get_pr_reviews(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        return list(self.reviews.get((repo, pr_number), []))

    def get_pr_review_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        return list(self.review_comments.get((repo, pr_number), []))


def test_post_evaluator_verdict_posts_full_review_on_pr_and_one_line_link_on_issue(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    gh = FakeGitHubForEvalUX(open_pr_num=54)

    verdict = Verdict(
        tenant="arsenyspb/ollama-monitor",
        pr=53,
        head_sha=HEAD_SHA,
        round=1,
        verdict="approved",
        findings=[],
    )

    ok = post_evaluator_verdict(
        gh=gh,
        tenant="arsenyspb/ollama-monitor",
        pr=53,
        verdict=verdict,
        current_pr_head_sha=HEAD_SHA,
    )
    assert ok is True
    assert len(gh.posted_comments) == 2

    issue_comment = next(c for c in gh.posted_comments if c["pr_number"] == 53)
    pr_comment = next(c for c in gh.posted_comments if c["pr_number"] == 54)

    # Issue #53 receives a concise one-line link to #54 + hidden flow-verdict envelope (not duplicate full review)
    assert "see full review on #54" in issue_comment["body"]
    assert "### 👉 Your Next Action" not in issue_comment["body"]
    assert "<!-- atobar-envelope" in issue_comment["body"]
    assert "```flow-verdict" in issue_comment["body"]

    # PR #54 receives the full review summary + Your Next Action footer
    assert "**ATOBAR Evaluator Review** approved `9261fc0`" in pr_comment["body"]
    assert "### 👉 Your Next Action" in pr_comment["body"]
    assert "@atobar-flow-agent" in pr_comment["body"]
    assert "@atobar-flow-agent eval" in pr_comment["body"]


def test_format_evaluator_next_action_states() -> None:
    v_approved = Verdict(
        tenant="arsenyspb/ollama-monitor",
        pr=53,
        head_sha=HEAD_SHA,
        round=1,
        verdict="approved",
        findings=[],
    )
    txt_app = format_evaluator_next_action(v_approved, max_review_rounds=4)
    assert "### 👉 Your Next Action" in txt_app
    assert "flow/arbiter" in txt_app
    assert "@atobar-flow-agent" in txt_app

    f1 = Finding(id="F1", criterion="AC-1", evidence="file:src/monitor_tui.py#L10", message="Fix chart")
    v_rework = Verdict(
        tenant="arsenyspb/ollama-monitor",
        pr=53,
        head_sha=HEAD_SHA,
        round=2,
        verdict="changes_requested",
        findings=[f1],
    )
    txt_rework = format_evaluator_next_action(v_rework, max_review_rounds=4)
    assert "Automatic rework in progress (Round 2/4)" in txt_rework
    assert "@atobar-flow-agent" in txt_rework

    v_capped = Verdict(
        tenant="arsenyspb/ollama-monitor",
        pr=53,
        head_sha=HEAD_SHA,
        round=4,
        verdict="changes_requested",
        findings=[f1],
    )
    txt_capped = format_evaluator_next_action(v_capped, max_review_rounds=4)
    assert "Review round cap reached (Round 4/4)" in txt_capped
    assert "flow:resume" in txt_capped
    assert "@atobar-flow-agent" in txt_capped


def test_human_comments_for_collects_issue_and_pr_comments_and_injects_into_all_roles() -> None:
    repo = "arsenyspb/ollama-monitor"
    gh = FakeGitHubForEvalUX(open_pr_num=54)
    gh.open_prs = [
        {
            "number": 54,
            "state": "open",
            "head": {"ref": "flow/issue-53", "sha": HEAD_SHA},
            "body": "Fixes #53",
        }
    ]
    gh.comments[(repo, 53)] = [
        {
            "id": 1,
            "body": "@atobar-flow-agent please include a 120-point history buffer",
            "user": {"login": "arsenyspb"},
            "author_association": "OWNER",
        },
        {
            "id": 2,
            "body": "**ATOBAR Evaluator Review** approved `9261fc0`.\n### 👉 Your Next Action\n`@atobar-flow-agent`",
            "user": {"login": "atobar-flow-agent[bot]", "type": "Bot"},
            "author_association": "CONTRIBUTOR",
        },
    ]
    gh.comments[(repo, 54)] = [
        {
            "id": 10,
            "body": "@atobar-flow-agent evaluate please verify the TUI with an idle 0-TPS period and step burst",
            "user": {"login": "arsenyspb"},
            "author_association": "OWNER",
        },
        {
            "id": 11,
            "body": "@atobar-flow-agent untrusted drive-by comment",
            "user": {"login": "mallory"},
            "author_association": "NONE",
        },
    ]
    gh.reviews[(repo, 54)] = [
        {
            "id": 20,
            "body": "@atobar-flow-agent please check the header alignment on 160x45 terminal",
            "user": {"login": "arsenyspb"},
            "author_association": "OWNER",
        }
    ]
    gh.review_comments[(repo, 54)] = [
        {
            "id": 30,
            "path": "src/monitor_tui.py",
            "line": 42,
            "body": "Avoid math.sin in test fixtures here",
            "user": {"login": "arsenyspb"},
            "author_association": "OWNER",
        }
    ]

    comments = _human_comments_for(
        gh,  # type: ignore[arg-type]
        repo,
        53,
        manifest={"owner_github_login": "arsenyspb"},
        head_sha=HEAD_SHA,
    )
    assert len(comments) == 4
    assert any("120-point history buffer" in c for c in comments)
    assert any("[PR #54] @arsenyspb: @atobar-flow-agent evaluate please verify the TUI" in c for c in comments)
    assert any("[PR #54 review] @arsenyspb: @atobar-flow-agent please check the header alignment" in c for c in comments)
    assert any("[PR #54 src/monitor_tui.py:42] @arsenyspb: Avoid math.sin in test fixtures here" in c for c in comments)
    assert not any("mallory" in c or "untrusted" in c for c in comments)
    assert not any("Your Next Action" in c for c in comments)

    # Verify injection into planner, developer, and evaluator contexts
    plan_ctx = build_planner_context(tenant=repo, pr=53, head_sha=HEAD_SHA, human_comments=comments)
    dev_ctx = build_developer_context(tenant=repo, human_comments=comments)
    eval_ctx = build_evaluator_context(
        tenant=repo,
        pr=53,
        head_sha=HEAD_SHA,
        issue_text="Build TUI",
        human_comments=comments,
    )
    for ctx in (plan_ctx, dev_ctx, eval_ctx):
        assert "120-point history buffer" in ctx
        assert "[PR #54 src/monitor_tui.py:42] @arsenyspb: Avoid math.sin in test fixtures here" in ctx
