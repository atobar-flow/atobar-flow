"""METRIC_COMPILER role (#301 WP-6): the model proposes, the runner measures."""

import os
import subprocess
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from flow_agent.llm.fake import FakeLLMClient
from flow_agent.llm.protocol import Reply
from flow_agent.roles.metric_compiler.runner import (
    MetricCompilationError,
    dry_run_metric,
    parse_compiled_metric,
    run_metric_compiler,
)


def _spec(run: str, **extra: Any) -> str:
    import json

    body = {"run": run, "comparator": "<=", "threshold": 0, "baseline_mode": "absolute", "tolerance_pct": None,
            "min_samples": 2, **extra}
    return f"Here you go:\n```json\n{json.dumps(body)}\n```\n"


def _git_repo(path: Any) -> str:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("# demo\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], check=True
    )
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def test_parse_forces_bound_key_and_drops_nulls():
    spec = parse_compiled_metric(_spec("echo 1", key="other_key"), "proxy_count")
    assert spec["key"] == "proxy_count"
    assert "tolerance_pct" not in spec
    assert spec["min_samples"] == 2


@pytest.mark.parametrize(
    "text",
    ["no json here", "```json\n[1, 2]\n```", _spec("echo 1", comparator="~="), _spec("   ")],
)
def test_parse_rejects_malformed_specs(text: str):
    with pytest.raises(MetricCompilationError):
        parse_compiled_metric(text, "k")


def test_dry_run_reads_last_stdout_line(tmp_path):
    value, tail = dry_run_metric("echo noise; echo 3.5", str(tmp_path))
    assert value == 3.5
    assert "noise" in tail


@pytest.mark.parametrize(
    "cmd",
    ["false | echo 1", "echo 1; echo done", "echo nan", "echo 1; echo", "exit 3"],
)
def test_dry_run_fails_like_ci(tmp_path, cmd: str):
    with pytest.raises(MetricCompilationError):
        dry_run_metric(cmd, str(tmp_path))


def test_dry_run_never_sees_credentials(tmp_path):
    env = {"FLOW_RUN_CREDENTIAL": "c", "FLOW_LLM_API_KEY_GEMINI": "k", "GITHUB_TOKEN": "g", "PATH": os.environ["PATH"]}
    with patch.dict(os.environ, env, clear=True):
        cmd = 'test -z "$FLOW_RUN_CREDENTIAL$FLOW_LLM_API_KEY_GEMINI$GITHUB_TOKEN" && echo 1'
        assert dry_run_metric(cmd, str(tmp_path))[0] == 1.0


def test_dry_run_timeout_kills_the_whole_process_group(tmp_path):
    import time

    start = time.monotonic()
    with pytest.raises(MetricCompilationError, match="timed out"):
        dry_run_metric("sleep 5; echo 1", str(tmp_path), timeout_s=0.1)
    assert time.monotonic() - start < 1.0  # a child holding stdout open must not block communicate()


def test_dry_run_rejects_and_reverts_tracked_mutations(tmp_path):
    _git_repo(tmp_path)
    with pytest.raises(MetricCompilationError, match="modified tracked files"):
        dry_run_metric("echo hacked > README.md; echo 1", str(tmp_path))
    assert (tmp_path / "README.md").read_text() == "# demo\n"


def test_dry_run_cleans_untracked_files_but_keeps_ignored_setup_outputs(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".venv/\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "ig"], check=True)
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "marker").write_text("x")
    assert dry_run_metric("touch junk.txt; echo 2", str(tmp_path))[0] == 2.0
    assert not (tmp_path / "junk.txt").exists()
    assert (tmp_path / ".venv" / "marker").exists()


@pytest.mark.parametrize("tol", [float("nan"), float("inf"), -1])
def test_parse_rejects_bad_tolerance(tol: float):
    import json

    body = json.dumps({"run": "echo 1", "comparator": "<=", "threshold": 0, "tolerance_pct": tol})
    with pytest.raises(MetricCompilationError, match="tolerance_pct"):
        parse_compiled_metric(f"```json\n{body}\n```", "k")


def test_parse_prefers_json_fence_over_earlier_shell_fence():
    text = "Try:\n```\n{\"run\": \"wrong\", \"comparator\": \">\", \"threshold\": 1}\n```\n" + _spec("echo right")
    assert parse_compiled_metric(text, "k")["run"] == "echo right"


@pytest.mark.asyncio
async def test_runner_never_executes_forbidden_commands(tmp_path):
    _git_repo(tmp_path)
    llm = FakeLLMClient(canned_replies=[
        Reply(text=_spec("cat /etc/atobar/cp.env | wc -l")),
        Reply(text=_spec("rm .github/workflows/flow-metrics.yml; echo 0")),
        Reply(text=_spec("echo 0")),
    ])
    env = {"CONTROL_PANEL_URL": "http://cp", "FLOW_RUN_ID": "r1", "FLOW_RUN_CREDENTIAL": "cred"}
    ok = httpx.Response(200, json={}, request=httpx.Request("POST", "http://cp"))
    real_dry_run = dry_run_metric
    executed: list[str] = []

    def spy(cmd: str, *a: Any) -> Any:
        executed.append(cmd)
        return real_dry_run(cmd, *a)

    with patch.dict(os.environ, env), \
         patch("flow_agent.roles.metric_compiler.runner.dry_run_metric", side_effect=spy), \
         patch("flow_agent.roles.metric_compiler.runner.httpx.post", return_value=ok):
        await run_metric_compiler(tenant="o/r", key="k", intent="x y z", workspace=str(tmp_path), llm=llm)
    assert executed == ["echo 0"]


@pytest.mark.asyncio
async def test_runner_retries_with_feedback_and_posts_measured_value(tmp_path):
    sha = _git_repo(tmp_path)
    llm = FakeLLMClient(canned_replies=[
        Reply(text=_spec("echo not-a-number")),
        Reply(text=_spec("echo rejected-by-fe; echo 7")),
        Reply(text=_spec("ls README.md | wc -l")),
    ])
    posted: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        posted.append({"url": url, **kwargs})
        code = 422 if len(posted) == 1 else 200
        return httpx.Response(code, json={"detail": "unsafe"}, request=httpx.Request("POST", url))

    env = {"CONTROL_PANEL_URL": "http://cp/", "FLOW_RUN_ID": "r1__proxy_count", "FLOW_RUN_CREDENTIAL": "cred"}
    with patch.dict(os.environ, env), patch("flow_agent.roles.metric_compiler.runner.httpx.post", side_effect=fake_post):
        outcome = await run_metric_compiler(
            tenant="o/r", key="proxy_count", intent="no proxy processes", workspace=str(tmp_path), llm=llm
        )

    assert outcome.head_sha == sha and outcome.envelope_text == ""
    assert len(llm.history) == 3
    last_msgs = llm.history[2]["messages"]
    assert "Attempt 2 failed: rejected by the Flow Engine" in last_msgs[-1].content  # type: ignore[union-attr]
    assert [p["url"] for p in posted] == ["http://cp/v1/runs/r1__proxy_count/metric-compilation"] * 2
    final = posted[-1]["json"]
    assert final["value"] == 1.0  # measured by the runner, not claimed by the model
    assert final["sha"] == sha
    assert final["compiled"]["key"] == "proxy_count"
    assert posted[-1]["headers"] == {"Authorization": "Bearer cred"}


@pytest.mark.asyncio
async def test_runner_gives_up_after_max_attempts_without_posting(tmp_path):
    _git_repo(tmp_path)
    llm = FakeLLMClient(default_reply=Reply(text=_spec("exit 1")))
    env = {"CONTROL_PANEL_URL": "http://cp", "FLOW_RUN_ID": "r1", "FLOW_RUN_CREDENTIAL": "cred"}
    with patch.dict(os.environ, env), patch("flow_agent.roles.metric_compiler.runner.httpx.post") as post:
        with pytest.raises(MetricCompilationError, match="after 2 attempts"):
            await run_metric_compiler(
                tenant="o/r", key="k", intent="x y z", workspace=str(tmp_path), llm=llm, max_attempts=2
            )
    post.assert_not_called()


@pytest.mark.asyncio
async def test_runner_does_not_retry_conflicts(tmp_path):
    _git_repo(tmp_path)
    llm = FakeLLMClient(default_reply=Reply(text=_spec("echo 0")))

    def conflict(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(409, json={"detail": "already active"}, request=httpx.Request("POST", url))

    env = {"CONTROL_PANEL_URL": "http://cp", "FLOW_RUN_ID": "r1", "FLOW_RUN_CREDENTIAL": "cred"}
    with patch.dict(os.environ, env), patch("flow_agent.roles.metric_compiler.runner.httpx.post", side_effect=conflict):
        with pytest.raises(httpx.HTTPStatusError):
            await run_metric_compiler(tenant="o/r", key="k", intent="x y z", workspace=str(tmp_path), llm=llm)
    assert len(llm.history) == 1


@pytest.mark.asyncio
async def test_runner_requires_key_and_intent(tmp_path):
    env = {"CONTROL_PANEL_URL": "http://cp", "FLOW_RUN_ID": "r1", "FLOW_RUN_CREDENTIAL": "cred"}
    with patch.dict(os.environ, env):
        with pytest.raises(MetricCompilationError, match="missing metric key or intent"):
            await run_metric_compiler(tenant="o/r", key="k", intent="  ", workspace=str(tmp_path), llm=FakeLLMClient())
