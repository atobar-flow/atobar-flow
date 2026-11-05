"""Unit tests for J52 WP-5 User-Zero bootstrapper (`init-app` and `infra`)."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import threading
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from atobar_ops import bootstrap, cli


def _args(**kwargs: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "org": None,
        "slug": None,
        "cp_url": None,
        "env_file": None,
        "force": False,
        "dry_run": False,
        "write": False,
        "timeout": 30,
        "json": True,
        "verbose": True,
        "max_chars": 8000,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_init_app_waits_for_legacy_slug_release_and_runs_manifest_flow(tmp_path: Path, monkeypatch: Any) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FLOW_GITHUB_ORG=atobar-flow\n"
        "CP_GITHUB_APP_SLUG=atobar-flow-agent\n"
        "CP_HOSTNAME=cp.example.invalid\n"
        "GITHUB_APP_ID=\n",
        encoding="utf-8",
    )

    app_checks: list[int] = []
    opened_urls: list[str] = []

    def fake_fetch_app(slug: str) -> dict[str, Any] | None:
        assert slug == "atobar-flow-agent"
        app_checks.append(1)
        if len(app_checks) == 1:
            return {"id": 4396774, "slug": slug, "owner": {"login": "arsenyspb", "type": "User"}}
        return None

    def fake_open_browser(url: str) -> None:
        opened_urls.append(url)
        if url.startswith("http://127.0.0.1:") and url.endswith("/start"):
            # Simulate the browser visiting /start, inspecting the manifest form, and hitting /callback
            def _drive_callback() -> None:
                r_start = httpx.get(url, timeout=5.0)
                assert r_start.status_code == 200
                assert "https://github.com/organizations/atobar-flow/settings/apps/new?state=" in r_start.text
                assert "https://cp.example.invalid/auth/callback" in r_start.text
                state_part = r_start.text.split("settings/apps/new?state=", 1)[1].split("'", 1)[0]
                cb_url = url.rsplit("/start", 1)[0] + "/callback?" + urllib.parse.urlencode(
                    {"code": "manifest_code_123", "state": state_part}
                )
                r_cb = httpx.get(cb_url, timeout=5.0)
                assert r_cb.status_code == 200

            t = threading.Thread(target=_drive_callback, daemon=True)
            t.start()

    def fake_exchange(code: str) -> dict[str, Any]:
        assert code == "manifest_code_123"
        return {
            "id": 9988776,
            "slug": "atobar-flow-agent",
            "pem": "FAKE-PEM-KEY-MATERIAL-FOR-UNIT-TEST",
            "client_id": "Iv23liNewClientId",
            "client_secret": "secret_oauth_xyz",
        }

    monkeypatch.setattr(bootstrap, "fetch_github_app", fake_fetch_app)
    monkeypatch.setattr(bootstrap, "_open_browser", fake_open_browser)
    monkeypatch.setattr(bootstrap, "exchange_manifest_code", fake_exchange)
    monkeypatch.setattr(bootstrap.timebox.Deadline, "sleep", lambda self, s: None)

    rc = bootstrap.cmd_init_app(_args(env_file=str(env_file)))
    assert rc == 0
    assert len(app_checks) == 2
    assert opened_urls[0] == "https://github.com/settings/apps/atobar-flow-agent/advanced"
    assert opened_urls[1].endswith("/start")

    saved = bootstrap.load_env_map(env_file)
    assert saved["GITHUB_APP_ID"] == "9988776"
    assert saved["GITHUB_OAUTH_CLIENT_ID"] == "Iv23liNewClientId"
    assert saved["GITHUB_OAUTH_CLIENT_SECRET"] == "secret_oauth_xyz"
    decoded_pem = base64.b64decode(saved["GITHUB_APP_PRIVATE_KEY_BASE64"]).decode("utf-8")
    assert "FAKE-PEM-KEY-MATERIAL-FOR-UNIT-TEST" in decoded_pem


def test_infra_sync_vars_skips_empty_secrets_and_upserts_tfc_and_gh(tmp_path: Path, monkeypatch: Any) -> None:
    env_file = tmp_path / ".env"
    pem_b64 = base64.b64encode(b"FAKE-PEM-KEY-MATERIAL-FOR-UNIT-TEST\n").decode("ascii")
    env_file.write_text(
        "FLOW_GITHUB_ORG=atobar-flow\n"
        "FLOW_FACTORY_REPO=atobar-flow/atobar-flow\n"
        "CP_HOSTNAME=cp.example.invalid\n"
        "GITHUB_APP_ID=9988776\n"
        f"GITHUB_APP_PRIVATE_KEY_BASE64={pem_b64}\n"
        "GITHUB_OAUTH_CLIENT_ID=Iv23liCid\n"
        "GITHUB_OAUTH_CLIENT_SECRET=csecret\n"
        "AWS_REGION=us-west-2\n"
        "AWS_ACCESS_KEY_ID=AKIAFAKE\n"
        "HCLOUD_TOKEN=\n"
        "SUPABASE_URL=https://proj123.supabase.co\n"
        "SUPABASE_SERVICE_KEY=skey\n"
        "SUPABASE_DB_PASSWORD=dbpw\n"
        "SUPABASE_PGUSER=postgres.proj123\n"
        "AWS_PACKER_ROLE_ARN=arn:aws:iam::111122223333:role/packer\n",
        encoding="utf-8",
    )

    plan = bootstrap.build_sync_plan(bootstrap.load_env_map(env_file))
    varset_keys = [v.key for v in plan["varset_vars"]]
    # HCLOUD_TOKEN is empty in .env so it must NOT be in varset_vars (preserving existing TFC var)
    assert "HCLOUD_TOKEN" not in varset_keys
    assert "AWS_ACCESS_KEY_ID" in varset_keys

    # Dry-run test
    rc_dry = bootstrap.cmd_infra_sync_vars(_args(env_file=str(env_file), write=False))
    assert rc_dry == 0

    # Write mode test
    tfctl_calls: list[list[str]] = []
    gh_calls: list[list[str]] = []

    def fake_run(cmd: list[str], *, timeout: float, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "tfctl":
            tfctl_calls.append(cmd)
            if "/workspaces" in cmd[2] and "--all" in cmd:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps(
                        {
                            "data": [
                                {"id": "ws-1", "attributes": {"name": "atobar-flow-foundations"}},
                                {"id": "ws-2", "attributes": {"name": "atobar-flow-identity"}},
                                {"id": "ws-3", "attributes": {"name": "atobar-flow-control-panel"}},
                                {"id": "ws-4", "attributes": {"name": "atobar-flow-control-panel-hetzner"}},
                            ]
                        }
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"data": []}), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def fake_gh_run(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        gh_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout='{"nameWithOwner":"atobar-flow/atobar-flow"}', stderr="")

    monkeypatch.setattr(bootstrap.timebox, "run", fake_run)
    monkeypatch.setattr(bootstrap.gh, "run", fake_gh_run)

    rc_write = bootstrap.cmd_infra_sync_vars(_args(env_file=str(env_file), write=True))
    assert rc_write == 0
    assert any("POST" in c for c in tfctl_calls)
    assert any(c[:2] == ["secret", "set"] and "AWS_PACKER_ROLE_ARN" in c for c in gh_calls)
    assert any(c[:2] == ["secret", "set"] and "CP_HOSTNAME" in c for c in gh_calls)
    assert any(c[:2] == ["secret", "set"] and "CP_PUBLIC_URL" in c for c in gh_calls)


def test_infra_up_and_destroy_dependency_order_and_secret_purge(tmp_path: Path, monkeypatch: Any) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FLOW_FACTORY_REPO=atobar-flow/atobar-flow\nAWS_REGION=us-west-2\n", encoding="utf-8")

    executed_tf: list[tuple[str, str]] = []
    purged_calls: list[str] = []

    def fake_run(cmd: list[str], *, timeout: float, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "tfctl":
            if "--all" in cmd:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps(
                        {
                            "data": [
                                {"id": "ws-1", "attributes": {"name": "atobar-flow-foundations"}},
                                {"id": "ws-2", "attributes": {"name": "atobar-flow-identity"}},
                                {"id": "ws-3", "attributes": {"name": "atobar-flow-control-panel"}},
                                {"id": "ws-4", "attributes": {"name": "atobar-flow-control-panel-hetzner"}},
                            ]
                        }
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"data": {"attributes": {"vcs-repo": None}}}), stderr=""
            )
        if cmd[0] == "aws" and "list-secrets" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps(
                    {"SecretList": [{"Name": "flow/control-panel/admin_token", "DeletedDate": "2026-11-05"}]}
                ),
                stderr="",
            )
        if cmd[0] == "aws" and "delete-secret" in cmd:
            purged_calls.append(cmd[cmd.index("--secret-id") + 1])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "terraform":
            chdir = cmd[1].removeprefix("-chdir=")
            subcmd = cmd[2]
            if subcmd in ("apply", "destroy"):
                executed_tf.append((chdir, subcmd))
            if subcmd == "output":
                if chdir.endswith("foundation"):
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        stdout=json.dumps(
                            {
                                "gha_packer_role_arn": {"value": "arn:aws:iam::111:role/packer"},
                                "gha_dispatcher_role_arn": {"value": "arn:aws:iam::111:role/disp"},
                            }
                        ),
                        stderr="",
                    )
                if chdir.endswith("control-panel-hetzner"):
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        stdout=json.dumps(
                            {
                                "server_ipv4": {"value": "203.0.113.42"},
                                "control_panel_url": {"value": "https://cp.example.invalid"},
                            }
                        ),
                        stderr="",
                    )
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(bootstrap.timebox, "run", fake_run)
    monkeypatch.setattr(
        bootstrap.gh,
        "run",
        lambda cmd, timeout=10.0: subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr=""),
    )

    rc_destroy = bootstrap.cmd_infra_destroy(_args(env_file=str(env_file), write=True))
    assert rc_destroy == 0
    assert [d for d, action in executed_tf if action == "destroy"] == [
        "infra/terraform/control-panel-hetzner",
        "infra/terraform/control-panel",
        "infra/terraform/identity",
        "infra/terraform/foundation",
    ]
    assert "flow/control-panel/admin_token" in purged_calls

    executed_tf.clear()
    rc_up = bootstrap.cmd_infra_up(_args(env_file=str(env_file), write=True))
    assert rc_up == 0
    assert [d for d, action in executed_tf if action == "apply"] == [
        "infra/terraform/foundation",
        "infra/terraform/identity",
        "infra/terraform/control-panel",
        "infra/terraform/control-panel-hetzner",
    ]
    env_after = bootstrap.load_env_map(env_file)
    assert env_after["AWS_PACKER_ROLE_ARN"] == "arn:aws:iam::111:role/packer"
    assert env_after["AWS_DISPATCHER_ROLE_ARN"] == "arn:aws:iam::111:role/disp"


def test_cli_parser_wires_init_app_and_infra_subcommands() -> None:
    parser = cli.build_parser()
    a1 = parser.parse_args(["init-app", "--dry-run"])
    assert a1.func is bootstrap.cmd_init_app
    a2 = parser.parse_args(["infra", "sync-vars"])
    assert a2.func is bootstrap.cmd_infra_sync_vars
    a3 = parser.parse_args(["infra", "up"])
    assert a3.func is bootstrap.cmd_infra_up
    a4 = parser.parse_args(["infra", "destroy"])
    assert a4.func is bootstrap.cmd_infra_destroy
