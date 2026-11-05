"""#301 WP-6 end-to-end: CP compile button -> FE Spot dispatch -> metric_compiler on the box -> FE stores it.

Crosses flow-engine and flow-agent in-process: the user-data FE hands to EC2 is decoded exactly as
cloud-init exports it, the agent runner dry-runs the proposed command on a real git checkout, and its
POST is routed into the FE internal API.
"""

import base64
import os
import subprocess
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from flow_agent.llm.fake import FakeLLMClient
from flow_agent.llm.protocol import Reply
from flow_agent.main import _decode_metric_intent
from flow_agent.roles.metric_compiler.runner import run_metric_compiler
from flow_engine import api as fe_api
from flow_engine.store import MemoryStore

TOKEN = "fe-secret-token-wp6"


class _EC2:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def describe_launch_template_versions(self, **kwargs: Any) -> dict[str, Any]:
        return {"LaunchTemplateVersions": [{"LaunchTemplateData": {"UserData": ""}}]}

    def run_instances(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"Instances": [{"InstanceId": "i-compiler"}]}


def _exports(user_data_b64: str) -> dict[str, str]:
    """Evaluates the user-data export header with bash, as cloud-init does."""
    script = base64.b64decode(user_data_b64).decode()
    out = subprocess.run(["bash", "-c", script + "\nenv -0"], capture_output=True, text=True, env={}, check=True)
    return dict(kv.split("=", 1) for kv in out.stdout.split("\0") if "=" in kv)


@pytest.mark.asyncio
async def test_compile_dispatch_to_compiled_metric(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FE_INTERNAL_TOKEN", TOKEN)
    store, ec2 = MemoryStore(), _EC2()
    fe_api.app.dependency_overrides[fe_api.get_store] = lambda: store
    fe_api.app.dependency_overrides[fe_api.get_ec2_client] = lambda: ec2
    fe_api.app.dependency_overrides[fe_api.get_github_client] = lambda: None
    client = TestClient(fe_api.app)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    try:
        tenant_id = client.post(
            "/internal/v1/tenants", headers=headers, json={"repo": "arsenyspb/ollama-monitor", "created_by": "arsenyspb"}
        ).json()["tenant_id"]
        tenant_obj = store.tenant_by_id(tenant_id)
        assert tenant_obj is not None
        store.update_tenant_manifest(tenant_id, {**tenant_obj.manifest, "default_branch": "master"}, "sha-init")
        intent = 'Monitor must not spawn a "proxy"; $(touch /tmp/pwned) is just text'
        assert client.put(
            f"/internal/v1/tenants/{tenant_id}/metrics/proxy_count", headers=headers, json={"intent": intent}
        ).status_code == 200

        r = client.post(f"/internal/v1/tenants/{tenant_id}/metrics/proxy_count/compile", headers=headers, json={})
        assert r.status_code == 200 and r.json()["dispatched"] is True
        run_id = r.json()["run_id"]

        env = _exports(ec2.calls[0]["UserData"])
        assert env["FLOW_ROLE"] == "metric_compiler"
        assert env["ISSUE_ID"] == "0"
        assert env["TARGET_REF"] == "master"
        assert env["FLOW_METRIC_KEY"] == "proxy_count"
        assert _decode_metric_intent(env["FLOW_METRIC_INTENT_B64"]) == intent

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        (tmp_path / "monitor.py").write_text("print('hi')\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c"], check=True
        )

        def route_to_fe(url: str, json: dict[str, Any], **kwargs: Any) -> httpx.Response:
            path = url.split("http://cp", 1)[1].replace("/v1/runs/", "/internal/v1/runs/", 1)
            resp = client.post(path, headers=headers, json=json)
            return httpx.Response(resp.status_code, content=resp.content, request=httpx.Request("POST", url))

        llm = FakeLLMClient(canned_replies=[
            Reply(text='```json\n{"run": "cat $FLOW_RUN_CREDENTIAL; echo 0", "comparator": "<=", "threshold": 0}\n```'),
            Reply(text='```json\n{"run": "grep -c proxy monitor.py || true", "comparator": "<=", "threshold": 0}\n```'),
        ])
        run_env = {"CONTROL_PANEL_URL": "http://cp", "FLOW_RUN_ID": run_id, "FLOW_RUN_CREDENTIAL": "cred"}
        with patch.dict(os.environ, run_env), \
             patch("flow_agent.roles.metric_compiler.runner.httpx.post", side_effect=route_to_fe):
            await run_metric_compiler(
                tenant="arsenyspb/ollama-monitor",
                key=env["FLOW_METRIC_KEY"],
                intent=_decode_metric_intent(env["FLOW_METRIC_INTENT_B64"]),
                workspace=str(tmp_path),
                llm=llm,
            )

        defn = store.get_metric_definition(tenant_id, "proxy_count")
        assert defn["status"] == "compiled"
        assert defn["compiled"]["run"] == "grep -c proxy monitor.py || true"
        assert defn["dry_run"]["value"] == 0.0
        assert len(defn["dry_run"]["sha"]) == 40
        assert len(llm.history) == 2  # the secret-referencing command was rejected by FE and retried
    finally:
        fe_api.app.dependency_overrides.clear()


def test_launch_rejects_shell_unsafe_metric_key():
    from atobar_contracts.github_rest import launch_spot_instance

    with pytest.raises(ValueError, match="invalid metric_key"):
        launch_spot_instance({"role": "metric_compiler", "metric_key": 'x"; reboot; "'}, ec2_client=_EC2())


@pytest.mark.parametrize(
    "payload",
    [{"target_ref": 'main"; reboot; "'}, {"target_ref": "-rf"}, {"target_ref": "a/../b"}, {"engine_ref": "$(id)"}],
)
def test_launch_rejects_shell_unsafe_refs(payload: dict[str, str]):
    from atobar_contracts.github_rest import launch_spot_instance

    with pytest.raises(ValueError, match="invalid (target|engine)_ref"):
        launch_spot_instance({"role": "developer", **payload}, ec2_client=_EC2())


def test_compiler_halting_before_attempt_one_is_visible(monkeypatch: pytest.MonkeyPatch):
    from atobar_contracts.store import pick_metric_compiler_session

    monkeypatch.setenv("FE_INTERNAL_TOKEN", TOKEN)
    store, ec2 = MemoryStore(), _EC2()
    fe_api.app.dependency_overrides[fe_api.get_store] = lambda: store
    fe_api.app.dependency_overrides[fe_api.get_ec2_client] = lambda: ec2
    fe_api.app.dependency_overrides[fe_api.get_github_client] = lambda: None
    client = TestClient(fe_api.app)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    try:
        tid = client.post("/internal/v1/tenants", headers=headers, json={"repo": "o/r", "created_by": "o"}).json()["tenant_id"]
        client.put(f"/internal/v1/tenants/{tid}/metrics/proxy_count", headers=headers, json={"intent": "no proxy"})
        run_id = client.post(f"/internal/v1/tenants/{tid}/metrics/proxy_count/compile", headers=headers, json={}).json()["run_id"]
        # setup / key fetch fails: register + terminate heartbeats never carried a current_todo
        for status in ("Running", "Halted"):
            r = client.post(f"/internal/v1/runs/{run_id}/heartbeat", headers=headers, json={
                "status": status, "instance_id": "i-real", "issue_id": 0,
                "halt_detail": "No model configured" if status == "Halted" else None,
            })
            assert r.status_code == 200
        rows = list(store.sessions.values()) if isinstance(store.sessions, dict) else list(store.sessions)
        sess = pick_metric_compiler_session(rows, tid, "proxy_count")
        assert sess is not None and sess["instance_id"] == "i-real"
        assert sess["status"] == "Halted" and "No model configured" in sess["halted_reason"]
        assert sess["current_todo"] == "compile:proxy_count"
        assert store.latest_metric_compiler_session(tid, "proxy_count")["instance_id"] == "i-real"
    finally:
        fe_api.app.dependency_overrides.clear()


def test_pick_ignores_dispatched_placeholders_and_other_keys():
    from atobar_contracts.store import pick_metric_compiler_session

    rows = [
        {"tenant_id": "t", "run_id": "a__k", "status": "dispatched", "current_todo": "compile:k", "heartbeat_at": "3"},
        {"tenant_id": "t", "run_id": "b__kx", "status": "Running", "current_todo": "compile:kx", "heartbeat_at": "4"},
        {"tenant_id": "t", "run_id": "c__k", "status": "Halted", "current_todo": None, "heartbeat_at": "2"},
        {"tenant_id": "u", "run_id": "d__k", "status": "Running", "current_todo": "compile:k", "heartbeat_at": "9"},
    ]
    assert pick_metric_compiler_session(rows, "t", "k")["run_id"] == "c__k"
