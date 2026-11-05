"""User-Zero bootstrapper (`init-app` and `infra`) for atobar-ops (J52 WP-5)."""

from __future__ import annotations

import argparse
import base64
import html
import http.server
import json
import os
import secrets
import subprocess
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from atobar_ops import gh, output, timebox

EXIT_OK = 0
EXIT_ERROR = 1

WORKSPACES_ORDER: tuple[tuple[str, str, str, str], ...] = (
    ("foundation", "infra/terraform/foundation", "TFC_WS_FOUNDATIONS", "atobar-flow-foundations"),
    ("identity", "infra/terraform/identity", "TFC_WS_IDENTITY", "atobar-flow-identity"),
    ("control-panel", "infra/terraform/control-panel", "TFC_WS_CONTROL_PANEL", "atobar-flow-control-panel"),
    (
        "control-panel-hetzner",
        "infra/terraform/control-panel-hetzner",
        "TFC_WS_HETZNER",
        "atobar-flow-control-panel-hetzner",
    ),
)


@dataclass(frozen=True)
class TfcVarSpec:
    key: str
    value: str
    category: str = "terraform"
    sensitive: bool = False
    hcl: bool = False


def resolve_env_file(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    cur = Path.cwd().resolve()
    for directory in [cur, *cur.parents]:
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return Path(".env")


def load_env_map(env_path: Path | None = None) -> dict[str, str]:
    path = env_path or resolve_env_file()
    out: dict[str, str] = {}
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip("'").strip('"')
            if k:
                out[k] = v
    for k, v in os.environ.items():
        if k not in out and v:
            out[k] = v
    return out


def upsert_env_keys(env_path: Path, updates: dict[str, str]) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    remaining = dict(updates)
    out_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, _ = stripped.partition("=")
            key = k.strip()
            if key in remaining:
                out_lines.append(f"{key}={remaining.pop(key)}")
                continue
        out_lines.append(line)
    for key, val in remaining.items():
        out_lines.append(f"{key}={val}")
    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def _open_browser(url: str) -> None:
    try:
        res = subprocess.run(["open", url], capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            return
    except Exception:
        pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


def fetch_github_app(slug: str) -> dict[str, Any] | None:
    resp = httpx.get(
        f"https://api.github.com/apps/{slug}",
        headers={"Accept": "application/vnd.github+json"},
        timeout=15.0,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else None


def build_app_manifest(
    *,
    app_slug: str,
    cp_url: str,
    redirect_url: str,
) -> dict[str, Any]:
    clean_cp = cp_url.rstrip("/")
    return {
        "name": app_slug,
        "url": clean_cp,
        "hook_attributes": {
            "url": f"{clean_cp}/healthz",
            "active": False,
        },
        "redirect_url": redirect_url,
        "callback_urls": [f"{clean_cp}/auth/callback"],
        "request_oauth_on_install": False,
        "setup_on_update": False,
        "public": True,
        "default_permissions": {
            "contents": "write",
            "issues": "write",
            "pull_requests": "write",
            "checks": "write",
            "actions": "write",
            "workflows": "write",
            "metadata": "read",
        },
        "default_events": [],
    }


def build_manifest_form_html(action_url: str, manifest: dict[str, Any]) -> str:
    manifest_json = json.dumps(manifest)
    escaped_action = html.escape(action_url, quote=True)
    escaped_manifest = html.escape(manifest_json, quote=True)
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Atobar Flow — Create GitHub App</title></head>"
        "<body style='font-family:sans-serif;padding:2rem'>"
        "<h3>Redirecting to GitHub App Manifest registration...</h3>"
        f"<form id='manifest-form' method='post' action='{escaped_action}'>"
        f"<input type='hidden' name='manifest' value='{escaped_manifest}'>"
        "<button type='submit' style='padding:0.6rem 1.2rem;font-size:1rem'>Continue to GitHub (Create App)</button>"
        "</form>"
        "<script>document.getElementById('manifest-form').submit();</script>"
        "</body></html>"
    )


def exchange_manifest_code(code: str) -> dict[str, Any]:
    resp = httpx.post(
        f"https://api.github.com/app-manifests/{code}/conversions",
        headers={"Accept": "application/vnd.github+json"},
        timeout=20.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected GitHub App manifest conversion response")
    return data


def _app_deletion_url(existing_app: dict[str, Any], slug: str) -> str:
    owner = existing_app.get("owner") or {}
    owner_login = str(owner.get("login") or "").strip()
    owner_type = str(owner.get("type") or "").strip()
    if owner_type.lower() == "organization" and owner_login:
        return f"https://github.com/organizations/{owner_login}/settings/apps/{slug}/advanced"
    return f"https://github.com/settings/apps/{slug}/advanced"


def cmd_init_app(args: argparse.Namespace) -> int:
    env_path = resolve_env_file(getattr(args, "env_file", None))
    env = load_env_map(env_path)
    org = (getattr(args, "org", None) or env.get("FLOW_GITHUB_ORG") or "atobar-flow").strip()
    slug = (getattr(args, "slug", None) or env.get("CP_GITHUB_APP_SLUG") or "atobar-flow-agent").strip()
    raw_cp_host = env.get("CP_HOSTNAME", "").strip().rstrip("/")
    composed_cp_url = (
        raw_cp_host if raw_cp_host.startswith(("http://", "https://")) else f"https://{raw_cp_host}"
    ) if raw_cp_host else ""
    cp_url = (
        getattr(args, "cp_url", None)
        or env.get("CP_PUBLIC_URL")
        or composed_cp_url
        or "https://cp.example.invalid"
    ).strip().rstrip("/")
    dry_run = bool(getattr(args, "dry_run", False))
    timeout = timebox.resolve_timeout(getattr(args, "timeout", None))
    deadline = timebox.Deadline(timeout)

    action_base = f"https://github.com/organizations/{org}/settings/apps/new"
    lines: list[str] = [
        f"target_org={org}",
        f"app_slug={slug}",
        f"callback_url={cp_url}/auth/callback",
        f"manifest_endpoint={action_base}",
    ]

    if dry_run:
        preview_manifest = build_app_manifest(
            app_slug=slug,
            cp_url=cp_url,
            redirect_url="http://127.0.0.1:<port>/callback",
        )
        lines.append("[dry-run] Would verify slug availability and run GitHub App Manifest flow.")
        output.emit(args, lines, data={"manifest": preview_manifest, "action_url": action_base})
        return EXIT_OK

    existing = fetch_github_app(slug)
    if existing is not None:
        owner_login = str((existing.get("owner") or {}).get("login") or "").strip()
        existing_id = str(existing.get("id") or "").strip()
        if owner_login.lower() == org.lower() and env.get("GITHUB_APP_ID") == existing_id and not getattr(args, "force", False):
            lines.append(f"GitHub App '{slug}' (id={existing_id}) is already owned by '{org}' and recorded in {env_path}.")
            output.emit(args, lines, data={"app_id": existing_id, "slug": slug, "owner": owner_login})
            return EXIT_OK

        del_url = _app_deletion_url(existing, slug)
        lines.append(
            f"GitHub App slug '{slug}' is currently claimed by '{owner_login}' (id={existing_id}). "
            f"Opening {del_url} — please delete the legacy App to release the slug."
        )
        _open_browser(del_url)
        while existing is not None:
            if deadline.expired():
                lines.append(f"Timed out waiting for GitHub App '{slug}' (owner={owner_login}) to be deleted.")
                output.emit(args, lines)
                return EXIT_ERROR
            deadline.sleep(3.0)
            existing = fetch_github_app(slug)
        lines.append(f"Confirmed: legacy GitHub App '{slug}' deleted (GET /apps/{slug} returned 404).")

    state_token = secrets.token_urlsafe(16)
    captured: dict[str, str] = {}
    event_done = threading.Event()

    class _ManifestCallbackHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *log_args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/start":
                port = self.server.server_port  # type: ignore[attr-defined]
                manifest = build_app_manifest(
                    app_slug=slug,
                    cp_url=cp_url,
                    redirect_url=f"http://127.0.0.1:{port}/callback",
                )
                body = build_manifest_form_html(f"{action_base}?state={state_token}", manifest).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/callback":
                qs = urllib.parse.parse_qs(parsed.query)
                code_val = (qs.get("code") or [""])[0].strip()
                state_val = (qs.get("state") or [""])[0].strip()
                if not code_val or state_val != state_token:
                    err_body = b"<html><body><h3>Invalid state or missing code</h3></body></html>"
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(err_body)))
                    self.end_headers()
                    self.wfile.write(err_body)
                    captured["error"] = "invalid callback state or missing code"
                    event_done.set()
                    return

                captured["code"] = code_val
                ok_body = (
                    b"<!doctype html><html><head><meta charset='utf-8'><title>Atobar Flow</title></head>"
                    b"<body><h2>GitHub App manifest received</h2>"
                    b"<p>Completing credential exchange in terminal. You may close this tab.</p></body></html>"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(ok_body)))
                self.end_headers()
                self.wfile.write(ok_body)
                event_done.set()
                return

            self.send_response(404)
            self.end_headers()

    server = http.server.HTTPServer(("127.0.0.1", 0), _ManifestCallbackHandler)
    port = server.server_port
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        start_url = f"http://127.0.0.1:{port}/start"
        lines.append(f"Opened {start_url} to register '{slug}' under organization '{org}'.")
        _open_browser(start_url)
        while not event_done.is_set():
            if deadline.expired():
                lines.append("Timed out waiting for GitHub App Manifest callback.")
                output.emit(args, lines)
                return EXIT_ERROR
            event_done.wait(timeout=min(1.0, max(0.1, deadline.remaining())))
    finally:
        server.shutdown()
        server.server_close()

    if "error" in captured or "code" not in captured:
        lines.append(f"Manifest callback failed: {captured.get('error', 'no code received')}")
        output.emit(args, lines)
        return EXIT_ERROR

    conv = exchange_manifest_code(captured["code"])
    app_id = str(conv.get("id") or "").strip()
    pem = str(conv.get("pem") or "").strip()
    client_id = str(conv.get("client_id") or "").strip()
    client_secret = str(conv.get("client_secret") or "").strip()
    created_slug = str(conv.get("slug") or slug).strip()
    if not (app_id and pem and client_id and client_secret):
        lines.append("GitHub App conversion response missing required credentials (id, pem, client_id, client_secret).")
        output.emit(args, lines)
        return EXIT_ERROR

    pem_b64 = base64.b64encode((pem + "\n").encode("utf-8")).decode("ascii")
    updates = {
        "CP_GITHUB_APP_SLUG": created_slug,
        "GITHUB_APP_ID": app_id,
        "GITHUB_APP_PRIVATE_KEY_BASE64": pem_b64,
        "GITHUB_OAUTH_CLIENT_ID": client_id,
        "GITHUB_OAUTH_CLIENT_SECRET": client_secret,
    }
    upsert_env_keys(env_path, updates)
    lines.append(
        f"Saved GITHUB_APP_ID={app_id}, GITHUB_OAUTH_CLIENT_ID={client_id}, "
        f"GITHUB_APP_PRIVATE_KEY_BASE64=<REDACTED>, GITHUB_OAUTH_CLIENT_SECRET=<REDACTED> to {env_path}."
    )
    lines.append(
        f"Next: install the app at https://github.com/apps/{created_slug}/installations/new "
        "and run `uv run --no-sync atobar-ops infra sync-vars --write`."
    )
    output.emit(
        args,
        lines,
        data={"app_id": app_id, "slug": created_slug, "client_id": client_id, "env_file": str(env_path)},
    )
    return EXIT_OK


def _decode_private_key(env: dict[str, str]) -> str:
    b64 = env.get("GITHUB_APP_PRIVATE_KEY_BASE64", "").strip()
    if b64:
        try:
            return base64.b64decode(b64).decode("utf-8").strip()
        except Exception:
            pass
    raw = env.get("GITHUB_APP_PRIVATE_KEY", "").strip()
    return raw.replace("\\n", "\n") if raw else ""


def _admin_logins_hcl(raw: str) -> str:
    stripped = raw.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        return stripped
    items = [x.strip() for x in stripped.split(",") if x.strip()] or ["arsenyspb"]
    return json.dumps(items)


def build_sync_plan(env: dict[str, str]) -> dict[str, Any]:
    tfc_org = env.get("TF_CLOUD_ORGANIZATION", "").strip() or "your-tfc-org"
    varset_id = env.get("TFC_VARSET_ID", "").strip() or "varset-unconfigured"
    ws_foundations = env.get("TFC_WS_FOUNDATIONS", "atobar-flow-foundations").strip() or "atobar-flow-foundations"
    ws_identity = env.get("TFC_WS_IDENTITY", "atobar-flow-identity").strip() or "atobar-flow-identity"
    ws_cp = env.get("TFC_WS_CONTROL_PANEL", "atobar-flow-control-panel").strip() or "atobar-flow-control-panel"
    ws_hetzner = (
        env.get("TFC_WS_HETZNER", "atobar-flow-control-panel-hetzner").strip()
        or "atobar-flow-control-panel-hetzner"
    )

    aws_region = env.get("AWS_REGION", "us-west-2").strip() or "us-west-2"
    gh_org = env.get("FLOW_GITHUB_ORG", "atobar-flow").strip() or "atobar-flow"
    factory_repo = env.get("FLOW_FACTORY_REPO", f"{gh_org}/atobar-flow").strip() or f"{gh_org}/atobar-flow"
    cp_hostname = env.get("CP_HOSTNAME", "").strip().removeprefix("https://").removeprefix("http://").rstrip("/")
    cp_public_url = env.get("CP_PUBLIC_URL", "").strip().rstrip("/")
    if not cp_hostname and cp_public_url:
        cp_hostname = cp_public_url.split("://", 1)[-1].split("/", 1)[0].strip()
    if not cp_public_url and cp_hostname:
        cp_public_url = f"https://{cp_hostname}"
    private_key_pem = _decode_private_key(env)

    sb_url = env.get("SUPABASE_URL", "").strip()
    if sb_url == "https://your-project.supabase.co":
        sb_url = ""
    sb_pguser = env.get("SUPABASE_PGUSER", "").strip()
    if sb_pguser == "postgres.your-project-ref":
        sb_pguser = ""

    varset_vars = [
        TfcVarSpec("AWS_REGION", aws_region, category="env", sensitive=False),
        TfcVarSpec("AWS_ACCESS_KEY_ID", env.get("AWS_ACCESS_KEY_ID", "").strip(), category="env", sensitive=True),
        TfcVarSpec("AWS_SECRET_ACCESS_KEY", env.get("AWS_SECRET_ACCESS_KEY", "").strip(), category="env", sensitive=True),
        TfcVarSpec("HCLOUD_TOKEN", env.get("HCLOUD_TOKEN", "").strip(), category="env", sensitive=True),
    ]

    workspace_vars: dict[str, list[TfcVarSpec]] = {
        ws_foundations: [
            TfcVarSpec("aws_region", aws_region),
            TfcVarSpec("factory_repo", factory_repo),
        ],
        ws_identity: [
            TfcVarSpec("aws_region", aws_region),
            TfcVarSpec("github_app_id", env.get("GITHUB_APP_ID", "").strip(), sensitive=True),
            TfcVarSpec("github_app_installation_id", env.get("GITHUB_APP_INSTALLATION_ID", "").strip(), sensitive=True),
            TfcVarSpec("github_app_private_key", private_key_pem, sensitive=True),
            TfcVarSpec("github_oauth_client_id", env.get("GITHUB_OAUTH_CLIENT_ID", "").strip(), sensitive=True),
            TfcVarSpec("github_oauth_client_secret", env.get("GITHUB_OAUTH_CLIENT_SECRET", "").strip(), sensitive=True),
            TfcVarSpec("secret_recovery_window_days", "0", hcl=True),
            TfcVarSpec("kms_deletion_window_days", "7", hcl=True),
        ],
        ws_cp: [
            TfcVarSpec("tfc_organization_name", tfc_org),
            TfcVarSpec("tfc_workspace_name", ws_cp),
            TfcVarSpec("tfc_foundations_workspace_name", ws_foundations),
            TfcVarSpec("tfc_identity_workspace_name", ws_identity),
            TfcVarSpec("aws_region", aws_region),
            TfcVarSpec("factory_repo", factory_repo),
            TfcVarSpec("supabase_api_url", sb_url, sensitive=True),
            TfcVarSpec("supabase_service_key", env.get("SUPABASE_SERVICE_KEY", "").strip(), sensitive=True),
            TfcVarSpec("supabase_db_password", env.get("SUPABASE_DB_PASSWORD", "").strip(), sensitive=True),
            TfcVarSpec(
                "supabase_pghost",
                env.get("SUPABASE_PGHOST", "").strip(),
                sensitive=True,
            ),
            TfcVarSpec("supabase_pgport", env.get("SUPABASE_PGPORT", "5432").strip()),
            TfcVarSpec("supabase_pguser", sb_pguser, sensitive=True),
            TfcVarSpec("secret_recovery_window_days", "0", hcl=True),
        ],
        ws_hetzner: [
            TfcVarSpec("aws_region", aws_region),
            TfcVarSpec("factory_repo", factory_repo),
            TfcVarSpec("cp_hostname", cp_hostname, sensitive=True),
            TfcVarSpec("location", env.get("HCLOUD_LOCATION", "fsn1").strip() or "fsn1"),
            TfcVarSpec("server_type", env.get("HCLOUD_SERVER_TYPE", "cx23").strip() or "cx23"),
            TfcVarSpec(
                "ssh_allowed_cidrs",
                env.get("SSH_ALLOWED_CIDRS", '["0.0.0.0/0"]').strip() or '["0.0.0.0/0"]',
                hcl=True,
            ),
            TfcVarSpec("cp_admin_logins", _admin_logins_hcl(env.get("CP_ADMIN_LOGINS", "arsenyspb")), hcl=True),
            TfcVarSpec("secret_recovery_window_days", "0", hcl=True),
            TfcVarSpec("ghcr_image_prefix", f"ghcr.io/{gh_org}"),
        ],
    }

    gh_secrets: dict[str, str] = {"AWS_REGION": aws_region}
    if env.get("AWS_PACKER_ROLE_ARN", "").strip():
        gh_secrets["AWS_PACKER_ROLE_ARN"] = env["AWS_PACKER_ROLE_ARN"].strip()
    if env.get("AWS_DISPATCHER_ROLE_ARN", "").strip():
        gh_secrets["AWS_DISPATCHER_ROLE_ARN"] = env["AWS_DISPATCHER_ROLE_ARN"].strip()
    if cp_hostname:
        gh_secrets["CP_HOSTNAME"] = cp_hostname
    if cp_public_url:
        gh_secrets["CP_PUBLIC_URL"] = cp_public_url

    return {
        "tfc_org": tfc_org,
        "varset_id": varset_id,
        "factory_repo": factory_repo,
        "varset_vars": [v for v in varset_vars if v.value],
        "workspace_vars": {ws: [v for v in specs if v.value] for ws, specs in workspace_vars.items()},
        "gh_secrets": gh_secrets,
        "gh_vars": gh_secrets,
    }


def _tfctl_json(args_list: list[str], *, timeout: float) -> Any:
    res = timebox.run(["tfctl", *args_list, "--json"], timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"tfctl {' '.join(args_list[:3])} failed: {res.stderr.strip()[:300]}")
    return json.loads(res.stdout) if res.stdout.strip() else {}


def _resolve_workspace_ids(tfc_org: str, *, timeout: float) -> dict[str, str]:
    doc = _tfctl_json(["api", "/organizations/{organization}/workspaces", "-p", f"organization={tfc_org}", "--all"], timeout=timeout)
    out: dict[str, str] = {}
    for item in doc.get("data") or []:
        if isinstance(item, dict):
            ws_id = str(item.get("id") or "")
            name = str((item.get("attributes") or {}).get("name") or "")
            if ws_id and name:
                out[name] = ws_id
    return out


def _upsert_tfc_var(
    *,
    list_endpoint: str,
    create_endpoint: str,
    patch_prefix: str,
    existing_vars: list[dict[str, Any]],
    spec: TfcVarSpec,
    timeout: float,
) -> str:
    match_id: str | None = None
    for item in existing_vars:
        attrs = item.get("attributes") or {}
        if attrs.get("key") == spec.key and attrs.get("category") == spec.category:
            match_id = str(item.get("id") or "")
            break

    payload = {
        "data": {
            "type": "vars",
            **({"id": match_id} if match_id else {}),
            "attributes": {
                "key": spec.key,
                "value": spec.value,
                "category": spec.category,
                "sensitive": spec.sensitive,
                "hcl": spec.hcl,
            },
        }
    }
    if match_id:
        res = timebox.run(
            ["tfctl", "api", f"{patch_prefix}/{match_id}", "-X", "PATCH", "-i", json.dumps(payload), "--json"],
            timeout=timeout,
        )
        action = "updated"
        if res.returncode != 0:
            timebox.run(["tfctl", "api", f"{patch_prefix}/{match_id}", "-X", "DELETE", "--json"], timeout=timeout)
            create_payload = {
                "data": {
                    "type": "vars",
                    "attributes": payload["data"]["attributes"],
                }
            }
            res = timebox.run(
                ["tfctl", "api", create_endpoint, "-X", "POST", "-i", json.dumps(create_payload), "--json"],
                timeout=timeout,
            )
            action = "recreated"
    else:
        res = timebox.run(
            ["tfctl", "api", create_endpoint, "-X", "POST", "-i", json.dumps(payload), "--json"],
            timeout=timeout,
        )
        action = "created"
    if res.returncode != 0:
        raise RuntimeError(f"failed to upsert {spec.key}: {res.stderr.strip()[:300]}")
    return action


def cmd_infra_sync_vars(args: argparse.Namespace) -> int:
    env_path = resolve_env_file(getattr(args, "env_file", None))
    env = load_env_map(env_path)
    plan = build_sync_plan(env)
    write_mode = bool(getattr(args, "write", False))
    timeout = timebox.resolve_timeout(getattr(args, "timeout", None))
    deadline = timebox.Deadline(timeout)

    lines: list[str] = [
        f"env_file={env_path}",
        f"tfc_organization={plan['tfc_org']}",
        f"varset_id={plan['varset_id']}",
        f"factory_repo={plan['factory_repo']}",
    ]

    for spec in plan["varset_vars"]:
        shown = "<REDACTED>" if spec.sensitive else spec.value
        prefix = "[sync]" if write_mode else "[dry-run]"
        lines.append(f"  {prefix} varset:{plan['varset_id']} {spec.category}:{spec.key}={shown}")

    for ws_name, specs in plan["workspace_vars"].items():
        for spec in specs:
            shown = "<REDACTED>" if spec.sensitive else spec.value
            prefix = "[sync]" if write_mode else "[dry-run]"
            lines.append(f"  {prefix} ws:{ws_name} {spec.category}:{spec.key}={shown}")

    for gh_k in plan["gh_secrets"]:
        prefix = "[sync]" if write_mode else "[dry-run]"
        lines.append(f"  {prefix} gh-secret:{plan['factory_repo']} {gh_k}=<REDACTED>")

    if not write_mode:
        lines.append("Pass `--write` to apply these secrets/variables to HCP Terraform and GitHub Actions.")
        output.emit(args, lines, data={"wrote": False, "tfc_org": plan["tfc_org"]})
        return EXIT_OK

    ws_ids = _resolve_workspace_ids(plan["tfc_org"], timeout=min(20.0, deadline.remaining()))
    varset_id = plan["varset_id"]
    existing_vs_doc = _tfctl_json(
        ["api", f"/varsets/{varset_id}/relationships/vars"],
        timeout=min(20.0, deadline.remaining()),
    )
    existing_vs_vars = existing_vs_doc.get("data") or []
    for spec in plan["varset_vars"]:
        _upsert_tfc_var(
            list_endpoint=f"/varsets/{varset_id}/relationships/vars",
            create_endpoint=f"/varsets/{varset_id}/relationships/vars",
            patch_prefix=f"/varsets/{varset_id}/relationships/vars",
            existing_vars=existing_vs_vars,
            spec=spec,
            timeout=min(15.0, deadline.remaining()),
        )

    for ws_name, specs in plan["workspace_vars"].items():
        ws_id = ws_ids.get(ws_name)
        if not ws_id:
            lines.append(f"ERROR: workspace '{ws_name}' not found in HCP Terraform org '{plan['tfc_org']}'")
            output.emit(args, lines)
            return EXIT_ERROR
        existing_ws_doc = _tfctl_json(["api", f"/workspaces/{ws_id}/vars"], timeout=min(20.0, deadline.remaining()))
        existing_ws_vars = existing_ws_doc.get("data") or []
        for spec in specs:
            _upsert_tfc_var(
                list_endpoint=f"/workspaces/{ws_id}/vars",
                create_endpoint=f"/workspaces/{ws_id}/vars",
                patch_prefix=f"/workspaces/{ws_id}/vars",
                existing_vars=existing_ws_vars,
                spec=spec,
                timeout=min(15.0, deadline.remaining()),
            )

    repo_check = gh.run(["repo", "view", plan["factory_repo"], "--json", "nameWithOwner"], timeout=10.0)
    if repo_check.returncode == 0:
        for gh_k, gh_v in plan["gh_secrets"].items():
            gh.run(
                ["secret", "set", gh_k, "--body", gh_v, "-R", plan["factory_repo"]],
                timeout=min(15.0, deadline.remaining()),
            )
    else:
        lines.append(f"note: GitHub repo {plan['factory_repo']} not reachable yet; skipped `gh secret set`.")

    lines.append("Successfully synced secrets and variables to HCP Terraform and GitHub Actions.")
    output.emit(args, lines, data={"wrote": True, "tfc_org": plan["tfc_org"]})
    return EXIT_OK


def _detach_vcs_if_needed(ws_id: str, *, timeout: float) -> bool:
    doc = _tfctl_json(["api", f"/workspaces/{ws_id}"], timeout=timeout)
    attrs = (doc.get("data") or {}).get("attributes") or {}
    if not attrs.get("vcs-repo") and not attrs.get("working-directory"):
        return False
    payload = json.dumps(
        {"data": {"type": "workspaces", "attributes": {"vcs-repo": None, "working-directory": ""}}}
    )
    res = timebox.run(
        ["tfctl", "api", f"/workspaces/{ws_id}", "-X", "PATCH", "-i", payload, "--json"],
        timeout=timeout,
    )
    if res.returncode != 0:
        raise RuntimeError(f"failed to detach vcs-repo on {ws_id}: {res.stderr.strip()[:300]}")
    return True


def _purge_scheduled_aws_secrets(region: str, *, timeout: float) -> list[str]:
    """Purges any `flow/*` secrets in AWS Secrets Manager scheduled for deletion (EC-3)."""
    res = timebox.run(
        [
            "aws",
            "secretsmanager",
            "list-secrets",
            "--include-planned-deletion",
            "--region",
            region,
            "--output",
            "json",
        ],
        timeout=timeout,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return []
    try:
        data = json.loads(res.stdout)
    except Exception:
        return []
    purged: list[str] = []
    for item in data.get("SecretList") or []:
        name = str(item.get("Name") or "")
        if not name.startswith("flow/") or not item.get("DeletedDate"):
            continue
        timebox.run(
            ["aws", "secretsmanager", "restore-secret", "--secret-id", name, "--region", region],
            timeout=15.0,
        )
        del_res = timebox.run(
            [
                "aws",
                "secretsmanager",
                "delete-secret",
                "--secret-id",
                name,
                "--force-delete-without-recovery",
                "--region",
                region,
            ],
            timeout=15.0,
        )
        if del_res.returncode == 0:
            purged.append(name)
    return purged


def _terraform_outputs(tf_dir: str, *, timeout: float) -> dict[str, Any]:
    res = timebox.run(["terraform", f"-chdir={tf_dir}", "output", "-json"], timeout=timeout)
    if res.returncode != 0 or not res.stdout.strip():
        return {}
    try:
        raw = json.loads(res.stdout)
    except Exception:
        return {}
    return {k: v.get("value") for k, v in raw.items() if isinstance(v, dict)}


def cmd_infra_up(args: argparse.Namespace) -> int:
    return _run_infra_orchestration(args, destroy=False)


def cmd_infra_destroy(args: argparse.Namespace) -> int:
    return _run_infra_orchestration(args, destroy=True)


def _run_infra_orchestration(args: argparse.Namespace, *, destroy: bool) -> int:
    env_path = resolve_env_file(getattr(args, "env_file", None))
    env = load_env_map(env_path)
    tfc_org = env.get("TF_CLOUD_ORGANIZATION", "").strip() or "your-tfc-org"
    aws_region = env.get("AWS_REGION", "us-west-2").strip() or "us-west-2"
    factory_repo = env.get("FLOW_FACTORY_REPO", "atobar-flow/atobar-flow").strip() or "atobar-flow/atobar-flow"
    write_mode = bool(getattr(args, "write", False))
    raw_timeout = getattr(args, "timeout", None)
    timeout = int(raw_timeout) if raw_timeout else 900
    deadline = timebox.Deadline(timeout)

    ordered = list(reversed(WORKSPACES_ORDER)) if destroy else list(WORKSPACES_ORDER)
    action_word = "destroy" if destroy else "apply"
    lines: list[str] = [f"mode={'destroy' if destroy else 'up'} write={write_mode} tfc_org={tfc_org}"]

    for idx, (label, tf_dir, env_key, default_ws) in enumerate(ordered, 1):
        ws_name = env.get(env_key, default_ws).strip() or default_ws
        prefix = f"[{action_word}]" if write_mode else "[dry-run]"
        lines.append(f"  {idx}. {prefix} {label} ({tf_dir} -> {ws_name})")

    if not write_mode:
        lines.append(f"Pass `--write` to execute `terraform {action_word}` across all 4 workspaces.")
        output.emit(
            args,
            lines,
            data={"wrote": False, "mode": action_word, "workspaces": [w[0] for w in ordered]},
        )
        return EXIT_OK

    ws_ids = _resolve_workspace_ids(tfc_org, timeout=min(20.0, deadline.remaining()))
    os.environ["TF_CLOUD_ORGANIZATION"] = tfc_org
    purged_pre = _purge_scheduled_aws_secrets(aws_region, timeout=min(20.0, deadline.remaining()))
    if purged_pre:
        lines.append(f"purged scheduled AWS secrets: {', '.join(purged_pre)}")

    outputs_summary: dict[str, Any] = {}
    for label, tf_dir, env_key, default_ws in ordered:
        ws_name = env.get(env_key, default_ws).strip() or default_ws
        ws_id = ws_ids.get(ws_name)
        if ws_id and _detach_vcs_if_needed(ws_id, timeout=min(15.0, deadline.remaining())):
            lines.append(f"detached legacy VCS link on workspace {ws_name} ({ws_id}) for CLI orchestration")

        init_res = timebox.run(
            ["terraform", f"-chdir={tf_dir}", "init", "-input=false"],
            timeout=min(120.0, max(30.0, deadline.remaining())),
        )
        if init_res.returncode != 0:
            lines.append(f"ERROR: terraform init failed in {tf_dir}: {init_res.stderr.strip()[-400:]}")
            output.emit(args, lines)
            return EXIT_ERROR

        tf_cmd = ["terraform", f"-chdir={tf_dir}", action_word, "-auto-approve", "-input=false"]
        run_res = timebox.run(tf_cmd, timeout=min(300.0, max(60.0, deadline.remaining())))
        if run_res.returncode != 0:
            lines.append(f"ERROR: terraform {action_word} failed in {tf_dir}: {run_res.stderr.strip()[-600:]}")
            output.emit(args, lines)
            return EXIT_ERROR
        lines.append(f"completed: {label} ({ws_name}) -> ok")

        if destroy:
            purged = _purge_scheduled_aws_secrets(aws_region, timeout=min(20.0, deadline.remaining()))
            if purged:
                lines.append(f"purged scheduled AWS secrets after {label}: {', '.join(purged)}")
        else:
            ws_out = _terraform_outputs(tf_dir, timeout=min(30.0, deadline.remaining()))
            outputs_summary[label] = ws_out
            if label == "foundation":
                role_updates: dict[str, str] = {}
                if ws_out.get("gha_packer_role_arn"):
                    role_updates["AWS_PACKER_ROLE_ARN"] = str(ws_out["gha_packer_role_arn"])
                if ws_out.get("gha_dispatcher_role_arn"):
                    role_updates["AWS_DISPATCHER_ROLE_ARN"] = str(ws_out["gha_dispatcher_role_arn"])
                if role_updates:
                    upsert_env_keys(env_path, role_updates)
                    repo_check = gh.run(["repo", "view", factory_repo, "--json", "nameWithOwner"], timeout=10.0)
                    if repo_check.returncode == 0:
                        for rk, rv in {**role_updates, "AWS_REGION": aws_region}.items():
                            gh.run(["secret", "set", rk, "--body", rv, "-R", factory_repo], timeout=15.0)
            elif label == "control-panel-hetzner":
                ipv4 = ws_out.get("server_ipv4")
                cp_url_out = ws_out.get("control_panel_url")
                if ipv4:
                    lines.append(f"server_ipv4={ipv4}")
                if cp_url_out:
                    lines.append(f"control_panel_url={cp_url_out}")

    output.emit(args, lines, data={"wrote": True, "mode": action_word, "outputs": outputs_summary})
    return EXIT_OK
