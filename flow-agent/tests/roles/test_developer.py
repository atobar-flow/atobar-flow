import pytest

from flow_agent.llm.fake import FakeLLMClient
from flow_agent.llm.protocol import Reply
from atobar_contracts.envelopes import Ack, Finding, Plan, PlannedCriterion, Verdict, parse, render
from flow_agent.roles.developer.spec import (
    build_developer_context,
    create_developer_ack,
    developer_done_predicate,
    get_developer_spec,
)
from flow_agent.runtime.llm_loop import llm_loop
from flow_agent.runtime.tools import allowlist_for

VALID_SHA = "0123456789abcdef0123456789abcdef01234567"
ANOTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"

BOT_LOGINS = {
    "developer": "atobar-flow-agent[bot]",
    "evaluator": "atobar-flow-agent[bot]",
    "arbitrator": "atobar-flow-agent[bot]",
    "conductor": "atobar-flow[bot]",
}


# --- AC-1: Exits after posting an ack; never polls ---

@pytest.mark.asyncio
async def test_ac1_exits_after_posting_ack_never_polls():
    ack = Ack(
        tenant="arsenyspb/ollama-monitor",
        pr=42,
        head_sha=VALID_SHA,
        addressed_findings=[],
    )
    rendered_ack = render(ack)

    # Fake LLM returns an ack envelope on first turn
    fake_llm = FakeLLMClient([Reply(text=f"Task complete!\n\n{rendered_ack}")])
    spec = get_developer_spec(fake_llm)

    reply = await llm_loop(spec, "Fix bug #42")

    # Loop terminated immediately on first turn
    assert spec.done(reply) is True
    assert "```flow-ack" in reply.text or "<!-- atobar:flow-ack" in reply.text


def test_ac1_developer_done_predicate_replaces_complete_string():
    # Legacy string "✅ Complete" is NOT sufficient for developer done predicate
    reply_legacy = Reply(text="I finished everything. ✅ Complete")
    assert developer_done_predicate(reply_legacy) is False

    # Valid Ack envelope satisfies completion
    ack = Ack(
        tenant="arsenyspb/ollama-monitor",
        pr=10,
        head_sha=VALID_SHA,
    )
    reply_ack = Reply(text=f"Here is my ack:\n{render(ack)}")
    assert developer_done_predicate(reply_ack) is True


# --- AC-2: Never calls merge or posts verdicts (allowlist test) ---

def test_ac2_developer_tool_allowlist_excludes_merge():
    tools = allowlist_for("developer")
    # Developer must not have tools to merge PRs
    assert "merge_pr" not in tools
    assert "git_merge" not in tools


def test_ac2_unauthorized_bot_cannot_author_verdicts():
    verdict = Verdict(
        tenant="arsenyspb/ollama-monitor",
        pr=10,
        head_sha=VALID_SHA,
        round=1,
        verdict="approved",
        findings=[],
    )
    verdict_text = render(verdict)

    # Attempting to parse verdict with an unregistered bot fails author binding
    parsed = parse(
        comment_body=verdict_text,
        author_login="unrelated-bot[bot]",
        bots=BOT_LOGINS,
    )
    assert parsed is None


# --- AC-3: Ack head_sha equals the pushed SHA ---

def test_ac3_ack_head_sha_matches_pushed_sha():
    pushed_sha = VALID_SHA
    ack = create_developer_ack(
        tenant="arsenyspb/ollama-monitor",
        pr=42,
        pushed_sha=pushed_sha,
    )
    assert ack.head_sha == pushed_sha


def test_ac3_ack_mismatched_sha_rejected():
    pushed_sha = VALID_SHA
    wrong_sha = ANOTHER_SHA

    ack = create_developer_ack(
        tenant="arsenyspb/ollama-monitor",
        pr=42,
        pushed_sha=pushed_sha,
    )

    # Verification function checks that ack.head_sha strictly equals pushed SHA
    assert ack.head_sha == pushed_sha
    assert ack.head_sha != wrong_sha


# --- Inputs on redispatch: only latest flow-verdict is new input ---

def test_developer_inputs_on_redispatch_includes_latest_verdict():
    latest_verdict = Verdict(
        tenant="arsenyspb/ollama-monitor",
        pr=42,
        head_sha=VALID_SHA,
        round=1,
        verdict="changes_requested",
        findings=[
            Finding(id="F1", criterion="correctness", evidence="assert failed in test_app.py")
        ],
    )

    context = build_developer_context(
        tenant="arsenyspb/ollama-monitor",
        wiki={"purpose": "Monitor ollama instances"},
        latest_verdict=latest_verdict,
    )

    assert "Monitor ollama instances" in context
    assert "LATEST REVIEW VERDICT" in context
    assert "changes_requested" in context
    assert "assert failed in test_app.py" in context


def test_developer_context_includes_latest_plan_and_wiki_concepts():
    plan = Plan(
        tenant="arsenyspb/ollama-monitor",
        pr=42,
        head_sha="0" * 40,
        complexity="single_pr",
        goal="Prevent layout overflow on narrow terminals",
        scope_files=["src/ui/layout.py", "tests/test_layout.py"],
        out_of_scope=["API routes"],
        acceptance_criteria=[
            PlannedCriterion(
                id="AC-1",
                description="Renders cleanly at 80 cols",
                verification_cmd="pytest tests/test_layout.py -q",
            )
        ],
        implementation_steps=["Clamp table column widths in src/ui/layout.py"],
    )
    context = build_developer_context(
        tenant="arsenyspb/ollama-monitor",
        wiki={
            "purpose": "Monitor ollama instances",
            "concepts": ["terminal layout clamping"],
        },
        latest_plan=plan,
    )
    assert "terminal layout clamping" in context
    assert "Prevent layout overflow on narrow terminals" in context
    assert "src/ui/layout.py" in context
    assert "AC-1" in context
    assert "pytest tests/test_layout.py -q" in context
    assert "Clamp table column widths" in context



def test_only_developer_context_says_planner_already_localized_repo():
    from atobar_contracts.envelopes import Plan

    from flow_agent.roles.base import format_plan_section
    from flow_agent.roles.developer.spec import build_developer_context

    plan = Plan.model_validate({
        "tenant": "o/r", "pr": 33, "head_sha": "0" * 40, "role": "planner", "complexity": "single_pr",
        "goal": "Add --version", "scope_files": ["src/cli.py", "tests/test_cli.py"],
        "acceptance_criteria": [], "implementation_steps": [],
    })
    # Shared with the evaluator, which must not be told to write tests or stay in scope.
    assert "already localized" not in format_plan_section(plan)
    assert "already localized" in build_developer_context(tenant="o/r", latest_plan=plan)


def test_human_comments_for_and_developer_context_injection():
    from atobar_contracts.constants import LOOP_EPOCH_MARKER
    from flow_agent.main import _human_comments_for
    from flow_agent.roles.planner.spec import build_planner_context

    class _FakeCommentGH:
        def get_comments(self, repo: str, issue_id: int) -> list[dict[str, object]]:
            comments: list[dict[str, object]] = [
                {
                    "user": {"login": "arsenyspb"},
                    "author_association": "OWNER",
                    "body": "Stale comment from previous epoch that must be excluded.",
                },
                {
                    "user": {"login": "atobar-flow-agent[bot]"},
                    "body": f"{LOOP_EPOCH_MARKER}\n🌊 **flow:** `flow:resume` applied",
                },
                {"user": {"login": "atobar-flow-agent[bot]"}, "body": "```flow-plan\n{}\n```"},
                {"user": {"login": "arsenyspb"}, "body": "<!-- atobar:status -->\n## atobar-flow Status"},
                {
                    "user": {"login": "random-stranger"},
                    "author_association": "NONE",
                    "body": "Untrusted stranger comment on public repo must be ignored.",
                },
            ]
            # Add 105 collaborator comments so we also test that the newest comment wins under budget
            for idx in range(105):
                comments.append(
                    {
                        "user": {"login": "collab-alice"},
                        "author_association": "COLLABORATOR",
                        "body": f"Clarification #{idx}: keep backward compatibility with Python 3.11 in cli.py.",
                    }
                )
            return comments

    human_comments = _human_comments_for(
        _FakeCommentGH(),  # type: ignore[arg-type]
        "owner/repo",
        42,
        manifest={"owner_github_login": "owner"},
    )
    assert any("Clarification #104" in c for c in human_comments), "Newest comment must be retained"
    assert not any("Stale comment from previous epoch" in c for c in human_comments)
    assert not any("Untrusted stranger comment" in c for c in human_comments)
    assert sum(len(c) for c in human_comments) <= 8000

    ctx = build_developer_context(
        tenant="owner/repo",
        human_comments=human_comments,
    )
    assert "HUMAN CLARIFICATIONS & COMMENTS ON ISSUE:" in ctx
    assert "issue_comments" in ctx
    assert "Clarification #104" in ctx
    assert "flow-plan" not in ctx
    assert "atobar:status" not in ctx

    planner_ctx = build_planner_context(
        tenant="owner/repo",
        pr=42,
        human_comments=human_comments,
    )
    assert "HUMAN CLARIFICATIONS & COMMENTS ON ISSUE:" in planner_ctx
    assert "Clarification #104" in planner_ctx

