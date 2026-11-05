import json
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from flow_agent.knowledge.onboard_repo import (
    SotaWiki,
    _gather_context,
    synthesize_wiki,
    upsert_wiki_to_control_plane,
)
from flow_agent.llm.fake import FakeLLMClient
from flow_agent.llm.protocol import Reply
from flow_agent.roles.base import format_wiki_section


def test_sotawiki_schema_validation_and_mermaid_fence_stripping():
    valid_data = {
        "purpose": "A test repo",
        "key_technologies": ["Python", "FastAPI"],
        "top_level_structure": "src/ and tests/",
        "key_concepts": ["REST"],
        "architecture_mermaid": "```mermaid\nflowchart TD\n  A[API] --> B[Store]\n```",
        "commit_sha": "a" * 40,
    }
    wiki = SotaWiki(**valid_data)
    assert wiki.purpose == "A test repo"
    assert wiki.technologies == ["Python", "FastAPI"]
    assert wiki.key_technologies == ["Python", "FastAPI"]
    assert wiki.structure == "src/ and tests/"
    assert wiki.top_level_structure == "src/ and tests/"
    assert wiki.concepts == ["REST"]
    assert wiki.key_concepts == ["REST"]
    assert wiki.architecture_mermaid == "flowchart TD\n  A[API] --> B[Store]"
    assert "```" not in wiki.architecture_mermaid


def test_sotawiki_schema_invalid():
    with pytest.raises(ValidationError):
        SotaWiki(purpose="A test repo")

    with pytest.raises(ValidationError, match="architecture_mermaid"):
        SotaWiki(
            purpose="A test repo",
            technologies=["Python"],
            structure="src/",
            concepts=["REST"],
            architecture_mermaid="not a valid mermaid diagram\njust prose",
        )

    with pytest.raises(ValidationError, match="architecture_mermaid"):
        SotaWiki(
            purpose="A test repo",
            technologies=["Python"],
            structure="src/",
            concepts=["REST"],
            architecture_mermaid="flowchart TD",  # header only, no body
        )

    with pytest.raises(ValidationError, match="double-quoted"):
        SotaWiki(
            purpose="A test repo",
            technologies=["Python"],
            structure="src/",
            concepts=["REST"],
            architecture_mermaid="flowchart TD\n  A[Control Panel (Hetzner)] --> B[Store]",
        )

    with pytest.raises(ValidationError, match="unbalanced subgraph/end"):
        SotaWiki(
            purpose="A test repo",
            technologies=["Python"],
            structure="src/",
            concepts=["REST"],
            architecture_mermaid='flowchart TD\n  subgraph Hub["Atobar Hub"]\n  A --> B',
        )

    with pytest.raises(ValidationError, match="unbalanced braces"):
        SotaWiki(
            purpose="A test repo",
            technologies=["Python"],
            structure="src/",
            concepts=["REST"],
            architecture_mermaid="erDiagram\n  TENANT {\n    uuid id PK",
        )


def test_format_wiki_section_renders_bullets_and_mermaid():
    sec = format_wiki_section(
        {
            "purpose": "Monitor local Ollama models",
            "technologies": ["Python", "FastAPI"],
            "structure": "src/main.py",
            "concepts": ["Async polling", "TUI"],
            "architecture_mermaid": "flowchart LR\n  UI --> Engine",
        }
    )
    assert "TENANT KNOWLEDGE / WIKI:" in sec
    assert "['Python'" not in sec
    assert "- Technologies:\n  * Python\n  * FastAPI" in sec
    assert "- Key Concepts:\n  * Async polling\n  * TUI" in sec
    assert "```mermaid\nflowchart LR\n  UI --> Engine\n```" in sec


def test_gather_context_captures_head_sha_and_agents_md(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "README.md").write_text("# My Repo\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# Agent Rules\nAlways run pytest.\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    g = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*g, "add", "."], check=True)
    subprocess.run([*g, "commit", "-qm", "init"], check=True)

    head_sha, readme, agents_md, tree, deps = _gather_context(str(tmp_path))
    assert len(head_sha) == 40
    assert "# My Repo" in readme
    assert "Always run pytest." in agents_md
    assert "README.md" in tree
    assert "pyproject.toml" in deps


def test_synthesize_wiki_retries_invalid_mermaid_and_records_commit_sha(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    g = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*g, "add", "."], check=True)
    subprocess.run([*g, "commit", "-qm", "init"], check=True)
    expected_sha = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()

    bad_reply = json.dumps(
        {
            "purpose": "Demo",
            "technologies": ["Python"],
            "structure": "README.md",
            "concepts": ["CLI"],
            "architecture_mermaid": "invalid diagram",
        }
    )
    good_reply = json.dumps(
        {
            "purpose": "Demo",
            "technologies": ["Python"],
            "structure": "README.md",
            "concepts": ["CLI"],
            "architecture_mermaid": "```mermaid\nflowchart TD\n  CLI --> Core\n```",
            "commit_sha": "f" * 40,  # model-supplied commit_sha must be overwritten by git rev-parse HEAD
        }
    )
    llm = FakeLLMClient(canned_replies=[Reply(text=bad_reply), Reply(text=good_reply)])
    wiki = synthesize_wiki(str(tmp_path), "owner/demo", llm=llm)
    assert wiki.commit_sha == expected_sha
    assert wiki.architecture_mermaid == "flowchart TD\n  CLI --> Core"
    assert len(llm.history) == 2
    assert not (tmp_path / ".atobar" / "wikis").exists()


def test_upsert_wiki_to_control_plane():
    wiki = SotaWiki(
        purpose="Wiki test",
        technologies=["Python"],
        structure="src/",
        concepts=["Modular"],
        architecture_mermaid="flowchart TD\n  A --> B",
        commit_sha="b" * 40,
    )
    env = {
        "CONTROL_PANEL_URL": "https://cp.example.test",
        "FLOW_RUN_ID": "run-123",
        "FLOW_RUN_CREDENTIAL": "cred-secret",
    }
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    with patch.dict(os.environ, env, clear=False), patch("httpx.post", return_value=mock_resp) as post_mock:
        success = upsert_wiki_to_control_plane("test-org/test-repo", wiki)
    assert success is True
    post_mock.assert_called_once()
    call_url = post_mock.call_args[0][0]
    payload = post_mock.call_args[1]["json"]
    assert call_url == "https://cp.example.test/v1/runs/run-123/wiki"
    assert payload["repo_name"] == "test-org/test-repo"
    assert payload["purpose"] == "Wiki test"
    assert payload["technologies"] == ["Python"]
    assert payload["architecture_mermaid"] == "flowchart TD\n  A --> B"
    assert payload["commit_sha"] == "b" * 40
