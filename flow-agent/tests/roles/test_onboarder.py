"""ONBOARDER role (#301 WP-6): CP-delivered model, loud failures, one onboarding PR."""

import json
import os
import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from flow_agent.knowledge.onboard_repo import synthesize_wiki
from flow_agent.llm.fake import FakeLLMClient
from flow_agent.llm.protocol import Reply
from flow_agent.roles.onboarder.runner import OnboardingError, run_onboarder

WIKI = {
    "purpose": "Monitor Ollama",
    "technologies": ["python"],
    "structure": "src/",
    "concepts": ["x"],
    "architecture_mermaid": "flowchart TD\n  CLI --> Monitor",
}
ENV = {"CONTROL_PANEL_URL": "http://cp", "FLOW_RUN_ID": "r-onb", "FLOW_RUN_CREDENTIAL": "cred", "ISSUE_ID": "1"}


def _repo_with_origin(tmp_path: Any) -> str:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    ws = tmp_path / "ws"
    subprocess.run(["git", "init", "-q", "-b", "main", str(ws)], check=True)
    (ws / "README.md").write_text("# demo\n")
    g = ["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*g, "add", "."], check=True)
    subprocess.run([*g, "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", str(ws), "remote", "add", "origin", str(origin)], check=True)
    subprocess.run(["git", "-C", str(ws), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(ws), "config", "user.email", "t@t"], check=True)
    return str(ws)


class _GH:
    def __init__(self) -> None:
        self.pulls: list[tuple[Any, ...]] = []

    def create_pull(self, *args: Any) -> dict[str, Any]:
        self.pulls.append(args)
        return {"number": 1}


@pytest.mark.asyncio
async def test_onboarder_publishes_wiki_and_opens_one_pr(tmp_path):
    ws = _repo_with_origin(tmp_path)
    llm = FakeLLMClient(canned_replies=[Reply(text="not json"), Reply(text=f"```json\n{json.dumps(WIKI)}\n```")])
    gh = _GH()
    with patch.dict(os.environ, ENV), \
         patch("flow_agent.roles.onboarder.runner.upsert_wiki_to_control_plane", return_value=True) as up:
        await run_onboarder(tenant="o/r", workspace=ws, manifest={}, gh_client=gh, llm=llm)  # type: ignore[arg-type]
    assert up.call_args[0][1].purpose == "Monitor Ollama"
    assert len(llm.history) == 2  # schema failure was fed back once
    assert gh.pulls and gh.pulls[0][:3] == ("o/r", "atobar/onboarding", "main")
    pushed = subprocess.run(["git", "-C", str(tmp_path / "origin.git"), "branch"], capture_output=True, text=True)
    assert "atobar/onboarding" in pushed.stdout


@pytest.mark.asyncio
async def test_onboarder_fails_loudly_when_wiki_publish_fails(tmp_path):
    ws = _repo_with_origin(tmp_path)
    llm = FakeLLMClient(default_reply=Reply(text=json.dumps(WIKI)))
    with patch.dict(os.environ, ENV), \
         patch("flow_agent.roles.onboarder.runner.upsert_wiki_to_control_plane", return_value=False):
        with pytest.raises(OnboardingError, match="SotaWiki"):
            await run_onboarder(tenant="o/r", workspace=ws, manifest={}, gh_client=_GH(), llm=llm)  # type: ignore[arg-type]


def test_synthesize_wiki_has_no_default_model(tmp_path):
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError, match="configure its model in the Control Panel"):
            synthesize_wiki(str(tmp_path), "o/r")


def test_synthesize_wiki_accepts_prose_before_the_json_fence(tmp_path):
    text = "Here is the wiki:\n```\nnot it\n```\n```json\n" + json.dumps(WIKI) + "\n```\nHope it helps."
    wiki = synthesize_wiki(str(tmp_path), "o/r", llm=FakeLLMClient(default_reply=Reply(text=text)))
    assert wiki.purpose == "Monitor Ollama"


@pytest.mark.asyncio
async def test_onboarder_wiki_only_when_issue_id_zero(tmp_path):
    ws = _repo_with_origin(tmp_path)
    llm = FakeLLMClient(default_reply=Reply(text=json.dumps(WIKI)))
    gh = _GH()
    env_zero = {**ENV, "ISSUE_ID": "0"}
    with patch.dict(os.environ, env_zero), \
         patch("flow_agent.roles.onboarder.runner.upsert_wiki_to_control_plane", return_value=True) as up:
        outcome = await run_onboarder(tenant="o/r", workspace=ws, manifest={}, gh_client=gh, llm=llm)  # type: ignore[arg-type]
    assert up.called
    published_wiki = up.call_args[0][1]
    assert published_wiki.purpose == "Monitor Ollama"
    assert len(published_wiki.commit_sha) == 40
    assert outcome.head_sha == published_wiki.commit_sha
    assert gh.pulls == []
    pushed = subprocess.run(["git", "-C", str(tmp_path / "origin.git"), "branch"], capture_output=True, text=True)
    assert "atobar/onboarding" not in pushed.stdout
