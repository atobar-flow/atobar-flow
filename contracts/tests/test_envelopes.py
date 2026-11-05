import ast
import pathlib

from atobar_contracts.envelopes import (
    Ack,
    Arbitration,
    Dispute,
    Finding,
    Plan,
    PlannedCriterion,
    Verdict,
    fingerprint,
    parse,
    render,
)

BOTS = {
    "planner": "atobar-flow-agent[bot]",
    "developer": "atobar-flow-agent[bot]",
    "evaluator": "atobar-flow-agent[bot]",
    "arbitrator": "atobar-flow-agent[bot]",
    "conductor": "flow-conductor[bot]",
}

VALID_SHA = "0123456789abcdef0123456789abcdef01234567"


def test_ac1_roundtrip_ack():
    ack = Ack(
        tenant="arsenyspb/comet-boundary",
        pr=42,
        head_sha=VALID_SHA,
        addressed_findings=["F1", "F2"],
        dispute=[Dispute(id="F3", reason="False positive")],
    )
    rendered = render(ack)
    assert "```flow-ack" in rendered

    # Plain render parse
    parsed = parse(rendered, author_login="atobar-flow-agent[bot]", bots=BOTS)
    assert parsed == ack

    # Surrounded by other markdown in comment
    comment = f"Hi team,\n\n{rendered}\n\nThanks!"
    parsed = parse(comment, author_login="atobar-flow-agent[bot]", bots=BOTS)
    assert parsed == ack


def test_ac1_roundtrip_verdict():
    finding = Finding(
        id="F1",
        criterion="AC-3",
        evidence="check_run:123456",
        severity="blocker",
        message="E2E login failed",
    )
    verdict = Verdict(
        tenant="arsenyspb/comet-boundary",
        pr=42,
        head_sha=VALID_SHA,
        round=2,
        verdict="changes_requested",
        findings=[finding],
    )
    rendered = render(verdict)
    assert "```flow-verdict" in rendered

    parsed = parse(rendered, author_login="atobar-flow-agent[bot]", bots=BOTS)
    assert parsed == verdict


def test_ac1_roundtrip_arbitration():
    arbitration = Arbitration(
        tenant="arsenyspb/comet-boundary",
        pr=42,
        head_sha=VALID_SHA,
        decision="pass",
        mode="recommend",
        action_taken="posted_recommendation",
        observation_ids=["obs_123", "obs_456"],
        summary="All metric gates passed.",
    )
    rendered = render(arbitration)
    assert "```flow-arbitration" in rendered

    parsed = parse(rendered, author_login="atobar-flow-agent[bot]", bots=BOTS)
    assert parsed == arbitration


def test_ac1_roundtrip_plan_and_sentinel_sha():
    sentinel_sha = "0" * 40
    plan = Plan(
        tenant="arsenyspb/ollama-monitor",
        pr=33,
        head_sha=sentinel_sha,
        run_id="run_plan_01",
        complexity="single_pr",
        goal="Fix dashboard overflow on mobile viewport",
        scope_files=["src/ui/dashboard.py", "tests/test_dashboard.py"],
        out_of_scope=["Backend API changes"],
        acceptance_criteria=[
            PlannedCriterion(
                id="AC-1",
                description="Dashboard container does not overflow horizontally at 375px",
                verification_cmd="pytest tests/test_dashboard.py -q",
            )
        ],
        implementation_steps=[
            "Update container CSS in src/ui/dashboard.py",
            "Add viewport regression test in tests/test_dashboard.py",
        ],
    )
    rendered = render(plan)
    assert "```flow-plan" in rendered

    parsed = parse(rendered, author_login="atobar-flow-agent[bot]", bots=BOTS)
    assert parsed == plan

    # Also works when bots dict only defines developer (fallback for legacy manifests)
    legacy_bots = {"developer": "atobar-flow-agent[bot]"}
    assert parse(rendered, author_login="atobar-flow-agent[bot]", bots=legacy_bots) == plan
    assert parse(rendered, author_login="intruder", bots=legacy_bots) is None



def test_ac2_wrong_author_returns_none():
    verdict = Verdict(
        tenant="arsenyspb/comet-boundary",
        pr=42,
        head_sha=VALID_SHA,
        round=1,
        verdict="approved",
        findings=[],
    )
    rendered = render(verdict)

    # Posted by a completely unrelated bot (since all roles now use the same app)
    assert parse(rendered, author_login="some-other-bot[bot]", bots=BOTS) is None
    # Posted by arbitrary human
    assert parse(rendered, author_login="alice", bots=BOTS) is None
    # Empty bots map
    assert parse(rendered, author_login="atobar-flow-agent[bot]", bots={}) is None


def test_ac3_bad_head_sha_returns_none():
    # Short SHA
    body = (
        '```flow-ack\n{"v":1,"role":"developer","tenant":"a/b","pr":1,'
        '"head_sha":"abc","addressed_findings":[]}\n```'
    )
    assert parse(body, author_login="atobar-flow-agent[bot]", bots=BOTS) is None

    # Uppercase SHA
    upper_sha = VALID_SHA.upper()
    body = (
        f'```flow-ack\n{{"v":1,"role":"developer","tenant":"a/b","pr":1,'
        f'"head_sha":"{upper_sha}","addressed_findings":[]}}\n```'
    )
    assert parse(body, author_login="atobar-flow-agent[bot]", bots=BOTS) is None

    # Non-hex SHA
    non_hex = "g" * 40
    body = (
        f'```flow-ack\n{{"v":1,"role":"developer","tenant":"a/b","pr":1,'
        f'"head_sha":"{non_hex}","addressed_findings":[]}}\n```'
    )
    assert parse(body, author_login="atobar-flow-agent[bot]", bots=BOTS) is None


def test_ac3_unknown_v_returns_none():
    body = (
        f'```flow-ack\n{{"v":2,"role":"developer","tenant":"a/b","pr":1,'
        f'"head_sha":"{VALID_SHA}","addressed_findings":[]}}\n```'
    )
    assert parse(body, author_login="atobar-flow-agent[bot]", bots=BOTS) is None


def test_ac3_extra_fields_returns_none():
    body = (
        f'```flow-ack\n{{"v":1,"role":"developer","tenant":"a/b","pr":1,'
        f'"head_sha":"{VALID_SHA}","addressed_findings":[],"unexpected":"forbid"}}\n```'
    )
    assert parse(body, author_login="atobar-flow-agent[bot]", bots=BOTS) is None


def test_ac4_fingerprint_stability():
    f1 = Finding(
        id="F1",
        criterion="AC-3",
        evidence="check_run:123456",
        message="Assertion failed: expected 200, got 500",
    )
    f2 = Finding(
        id="F2",
        criterion="  ac-3  ",
        evidence="CHECK_RUN:999999",  # Same evidence kind (check_run), different run ID
        message="assertion failed:  expected 200,   got 500  ",
    )
    assert fingerprint(f1) == fingerprint(f2)
    assert f1.fingerprint == f2.fingerprint
    assert f1.fingerprint.startswith("sha256:")

    # Different criterion or message produces different fingerprint
    f3 = Finding(
        id="F3",
        criterion="AC-4",
        evidence="check_run:123456",
        message="Assertion failed: expected 200, got 500",
    )
    assert fingerprint(f1) != fingerprint(f3)

    f4 = Finding(
        id="F4",
        criterion="AC-3",
        evidence="check_run:123456",
        message="Different message",
    )
    assert fingerprint(f1) != fingerprint(f4)


def test_ac5_no_llm_imports_in_protocol():
    protocol_dir = pathlib.Path(__file__).resolve().parents[1] / "src" / "atobar_contracts"
    llm_keywords = ["google.genai", "google.generativeai", "openai", "anthropic", "litellm", "langchain"]

    for py_file in protocol_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for forbidden in llm_keywords:
                        assert forbidden not in alias.name
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    for forbidden in llm_keywords:
                        assert forbidden not in node.module


def test_render_hidden_wraps_in_html_comment_and_roundtrips_via_parse():
    """#361: render_hidden wraps the fenced JSON in an HTML comment invisible on GitHub while parse() still extracts it."""
    import re
    from atobar_contracts.envelopes import render_hidden

    ack = Ack(tenant="owner/repo", pr=53, head_sha=VALID_SHA, run_id="run-1")
    hidden = render_hidden(ack)
    assert hidden.startswith("<!-- atobar-envelope\n")
    assert hidden.rstrip().endswith("-->")
    # Strip HTML comments as GitHub's Markdown renderer does -> no visible JSON or code fence remains
    visible = re.sub(r"<!--.*?-->", "", hidden, flags=re.DOTALL).strip()
    assert visible == ""
    assert parse(hidden, author_login="atobar-flow-agent[bot]", bots=BOTS) == ack

    # Arrow notation (-->) inside any field must be escaped so it does not close the HTML comment early
    verdict = Verdict(
        tenant="owner/repo",
        pr=53,
        head_sha=VALID_SHA,
        run_id="run-eval-1",
        round=1,
        verdict="changes_requested",
        findings=[Finding(id="F1", criterion="AC-1", evidence="src/app.py:1", message="a --> b")],
    )
    hidden_verdict = render_hidden(verdict)
    assert hidden_verdict.count("-->") == 1
    assert r"\u003e" in hidden_verdict
    assert re.sub(r"<!--.*?-->", "", hidden_verdict, flags=re.DOTALL).strip() == ""
    parsed_verdict = parse(hidden_verdict, author_login="atobar-flow-agent[bot]", bots=BOTS)
    assert parsed_verdict == verdict
    assert isinstance(parsed_verdict, Verdict)
    assert parsed_verdict.findings[0].message == "a --> b"

