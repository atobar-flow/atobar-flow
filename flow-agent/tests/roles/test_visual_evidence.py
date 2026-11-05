import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from atobar_contracts.envelopes import Ack, Finding, Verdict, parse, render
from flow_agent.roles.developer.spec import create_developer_ack
from flow_agent.roles.evaluator.spec import create_evaluator_verdict, validate_evaluator_finding
from flow_agent.roles.evaluator.visual import (
    capture_evaluator_screenshot,
    record_visual_artifact,
)
from flow_agent.runtime.tools import allowlist_for

SAMPLE_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n"
    b"-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"

BOT_LOGINS = {
    "developer": "atobar-flow-agent[bot]",
    "evaluator": "atobar-flow-agent[bot]",
    "arbitrator": "atobar-flow-agent[bot]",
    "conductor": "atobar-flow[bot]",
}


# --- AC-1: A screenshot artifact digest round-trips into the verdict ---

def test_ac1_screenshot_artifact_digest_roundtrips_into_verdict(tmp_path):
    artifact = record_visual_artifact(SAMPLE_PNG_BYTES, artifacts_dir=tmp_path)

    expected_hash = hashlib.sha256(SAMPLE_PNG_BYTES).hexdigest()
    assert artifact.sha256 == f"sha256:{expected_hash}"
    assert artifact.evidence == f"artifact:sha256:{expected_hash}"

    # Build finding and ensure evidence validation passes
    finding = Finding(
        id="F-UI-01",
        criterion="AC-UI",
        evidence=artifact.evidence,
        severity="major",
        message="Visual regression in web UI layout",
    )
    assert validate_evaluator_finding(finding) is True

    # Create verdict and render
    verdict = create_evaluator_verdict(
        tenant="arsenyspb/ollama-monitor",
        pr=42,
        head_sha=HEAD_SHA,
        round=1,
        verdict="changes_requested",
        findings=[finding],
    )
    rendered_comment = render(verdict)
    assert artifact.evidence in rendered_comment

    # Parse rendered comment back with evaluator bot author binding
    parsed = parse(
        comment_body=rendered_comment,
        author_login=BOT_LOGINS["evaluator"],
        bots=BOT_LOGINS,
    )
    assert isinstance(parsed, Verdict)
    assert len(parsed.findings) == 1
    assert parsed.findings[0].evidence == artifact.evidence
    assert parsed.findings[0].evidence == f"artifact:sha256:{expected_hash}"


def test_ac1_capture_evaluator_screenshot_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    with patch("flow_agent.tools.vision_tools.capture_screen_bytes", return_value=SAMPLE_PNG_BYTES):
        res = capture_evaluator_screenshot()

    expected_hash = hashlib.sha256(SAMPLE_PNG_BYTES).hexdigest()
    assert res["sha256"] == f"sha256:{expected_hash}"
    assert res["evidence"] == f"artifact:sha256:{expected_hash}"
    assert Path(res["path"]).is_file()


def test_workspace_dir_used_for_default_artifacts_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    with patch("flow_agent.tools.vision_tools.capture_screen_bytes", return_value=SAMPLE_PNG_BYTES):
        res = capture_evaluator_screenshot()

    assert str(tmp_path) in res["path"]
    assert Path(res["path"]).is_file()



# --- AC-2: No screenshot is attached by the developer role ---

def test_ac2_developer_tool_allowlist_excludes_visual_artifact_capture():
    dev_tools = allowlist_for("developer")
    assert "capture_evaluator_screenshot" not in dev_tools
    assert "record_visual_artifact" not in dev_tools


def test_ac2_developer_ack_cannot_attach_screenshots():
    # create_developer_ack does not accept screenshot or artifact evidence
    ack = create_developer_ack(
        tenant="arsenyspb/ollama-monitor",
        pr=42,
        pushed_sha=HEAD_SHA,
        addressed_findings=["F-UI-01"],
    )
    assert not hasattr(ack, "screenshots")
    assert not hasattr(ack, "artifact_sha256")
    assert not hasattr(ack, "evidence")

    # Developer cannot inject extra fields into Ack envelope (Pydantic extra='forbid')
    with pytest.raises(Exception):
        Ack(
            tenant="arsenyspb/ollama-monitor",
            pr=42,
            head_sha=HEAD_SHA,
            screenshots=["artifact:sha256:1234"],  # type: ignore[call-arg]
        )
