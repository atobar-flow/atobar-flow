import base64
import hashlib
import json
import os
import re
import struct
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Union

import httpx
import websockets
from bs4 import BeautifulSoup
from PIL import Image, ImageGrab

from flow_agent.core.untrusted import wrap_untrusted

DEFAULT_ARTIFACTS_DIR = Path(".atobar/artifacts")
DataMode = Literal["live", "synthetic", "hybrid", "undeclared"]
_VALID_DATA_MODES: frozenset[str] = frozenset({"live", "synthetic", "hybrid", "undeclared"})
_CMD_SECRET_RE = re.compile(
    r"(?i)(?:--?(?:api[-_]?key|token|secret|password|passwd|auth|bearer|credential)[=\s]+\S+"
    r"|\b(?:sk|ghp|ghs|gho|ghu|github_pat|atb_run|xox[baprs]|AIza)[A-Za-z0-9_.-]+"
    r"|[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:@]+:[^\s@]+@[^\s]+"
    r"|\b[A-Z0-9_]*(?:TOKEN|SECRET|KEY|PASSWORD|CREDENTIAL|DATABASE_URL)[A-Z0-9_]*=\S+)"
)


def _redact_command_secrets(text: str) -> str:
    return _CMD_SECRET_RE.sub("[REDACTED]", text or "")


def resolve_default_artifacts_dir() -> Path:
    """Resolve the default artifacts directory via workspace_dir() (FLOW_WORKSPACE_DIR -> WORKSPACE_DIR -> CWD)."""
    from flow_agent.tools.git_tools import workspace_dir

    ws = (
        os.environ.get("FLOW_WORKSPACE_DIR", "").strip()
        or os.environ.get("WORKSPACE_DIR", "").strip()
    )
    if ws:
        resolved_ws = workspace_dir() if os.path.exists(ws) else ws
        return Path(resolved_ws).expanduser().resolve() / DEFAULT_ARTIFACTS_DIR
    return DEFAULT_ARTIFACTS_DIR


def _extract_png_resolution(data: bytes) -> str:
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
        try:
            w, h = struct.unpack(">II", data[16:24])
            if w > 0 and h > 0:
                return f"{w}x{h}"
        except Exception:
            pass
    try:
        with Image.open(BytesIO(data)) as im:
            w, h = im.size
            if w > 0 and h > 0:
                return f"{w}x{h}"
    except Exception:
        pass
    return ""


def _collect_display_provenance(
    display: str = ":0",
    data: Optional[bytes] = None,
) -> dict[str, Any]:
    """Collects deterministic OS-level X11 window & process provenance at screenshot capture time."""
    x11_windows: list[str] = []
    has_active_window = True
    env = {**os.environ, "DISPLAY": display or ":0"}
    try:
        proc = subprocess.run(
            ["xwininfo", "-root", "-children"],
            env=env,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if proc.returncode == 0:
            for raw_line in (proc.stdout or "").splitlines():
                line = raw_line.strip()
                if not line.startswith("0x"):
                    continue
                m_geom = re.search(r"\b(\d+)x(\d+)\+-?\d+\+-?\d+", line)
                if m_geom:
                    w_px, h_px = int(m_geom.group(1)), int(m_geom.group(2))
                    if w_px <= 10 and h_px <= 10 and "(has no name)" in line:
                        continue
                x11_windows.append(line[:500])
                if len(x11_windows) >= 10:
                    break
            has_active_window = len(x11_windows) > 0
    except Exception:
        has_active_window = True

    active_processes: list[str] = []
    try:
        ps_proc = subprocess.run(
            ["ps", "-eo", "pid,etimes,args"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if ps_proc.returncode == 0:
            ws_hint = (
                os.environ.get("FLOW_WORKSPACE_DIR", "").strip()
                or os.environ.get("WORKSPACE_DIR", "").strip()
            )
            skip_patterns = (
                "ps -eo",
                "xwininfo",
                "flow_agent.main",
                "flow_agent.tools_mcp",
                "/sbin/",
                "/lib/systemd",
                "sshd:",
                "Xvfb",
                "openbox",
                "fluxbox",
                "dbus-daemon",
                "atobar-git-credential",
            )
            if ws_hint:
                for raw_line in (ps_proc.stdout or "").splitlines()[1:]:
                    line = " ".join(raw_line.strip().split())
                    if not line:
                        continue
                    parts = line.split(None, 2)
                    if len(parts) < 3:
                        continue
                    cmd = parts[2]
                    if cmd.startswith("[") or any(sp in cmd for sp in skip_patterns):
                        continue
                    if ws_hint in cmd:
                        active_processes.append(_redact_command_secrets(line)[:500])
                        if len(active_processes) >= 10:
                            break
    except Exception:
        pass

    resolution = _extract_png_resolution(data) if data else ""
    return {
        "x11_windows": x11_windows[:10],
        "active_processes": active_processes[:10],
        "has_active_window": has_active_window,
        "resolution": resolution,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def _clean_md_cell(value: Any, fallback: str = "—") -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return fallback
    text = text.replace("`", "'").replace("|", "/").replace("<", "&lt;").replace(">", "&gt;")
    return text[:500]


def render_screenshot_with_provenance(entry: dict[str, Any]) -> str:
    """Renders a screenshot image tag followed by a collapsible Verification Provenance <details> block."""
    url = str(entry.get("url") or "").strip()
    md = str(entry.get("markdown") or "").strip()
    img_line = f"![Screenshot]({url})" if url else (md or "![Screenshot]()")

    raw_meta = entry.get("metadata")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    raw_mode = str(entry.get("data_mode") or meta.get("data_mode") or "undeclared").strip().lower()
    data_mode = raw_mode if raw_mode in _VALID_DATA_MODES else "undeclared"
    mode_badge = {
        "synthetic": "🧪 SYNTHETIC DATA",
        "hybrid": "🔀 HYBRID DATA",
        "live": "🟢 LIVE DATA",
        "undeclared": "⚪ UNDECLARED",
    }[data_mode]

    data_source = _clean_md_cell(
        entry.get("data_source") or meta.get("data_source"),
        fallback="undeclared",
    )
    constraints = _clean_md_cell(
        entry.get("constraints") or meta.get("constraints"),
        fallback="none declared",
    )
    scenario = _clean_md_cell(
        entry.get("scenario") or meta.get("scenario"),
        fallback="undeclared",
    )

    raw_x11 = entry.get("x11_windows")
    if not isinstance(raw_x11, list):
        raw_x11 = meta.get("x11_windows")
    x11_windows: list[Any] = raw_x11 if isinstance(raw_x11, list) else []
    window_cell = _clean_md_cell("; ".join(str(w) for w in x11_windows[:3]), fallback="DISPLAY=:0 framebuffer")
    window_short = _clean_md_cell(x11_windows[0] if x11_windows else "DISPLAY=:0", fallback="DISPLAY=:0")[:80]

    sha_raw = str(entry.get("sha256") or meta.get("sha256") or "").strip()
    sha_hex = sha_raw[7:] if sha_raw.startswith("sha256:") else sha_raw
    sha_short = sha_hex[:12] if sha_hex else "unknown"
    sha_display = f"sha256:{sha_hex[:16]}..." if len(sha_hex) > 16 else (sha_raw or "sha256:unknown")

    run_id = _clean_md_cell(
        entry.get("run_id") or meta.get("run_id") or os.environ.get("FLOW_RUN_ID") or "local",
        fallback="local",
    )
    resolution = _clean_md_cell(entry.get("resolution") or meta.get("resolution"), fallback="unknown")
    size_bytes = entry.get("size_bytes") or meta.get("size_bytes")
    size_str = f"{int(size_bytes):,} bytes" if isinstance(size_bytes, int) and size_bytes > 0 else "recorded"

    return (
        f"{img_line}\n"
        f"<details>\n"
        f"<summary>🔍 Verification Provenance — Mode: <code>{mode_badge}</code> · "
        f"Window: <code>{window_short}</code> · SHA-256: <code>{sha_short}</code></summary>\n\n"
        f"| Field | Value |\n"
        f"| :--- | :--- |\n"
        f"| **Data Mode (declared by agent)** | `{data_mode}` |\n"
        f"| **Data Source (declared by agent)** | `{data_source}` |\n"
        f"| **Constraints / Conditions (declared by agent)** | `{constraints}` |\n"
        f"| **Scenario Tested (declared by agent)** | `{scenario}` |\n"
        f"| **X11 Window (`DISPLAY=:0`)** | `{window_cell}` |\n"
        f"| **Artifact** | `{run_id}` · `{sha_display}` (`{resolution}`, `{size_str}`) |\n\n"
        f"</details>"
    )


@dataclass(frozen=True)
class VisualArtifact:
    """Represents a captured visual artifact."""

    sha256: str
    evidence: str
    path: str
    size_bytes: int
    url: str = ""
    markdown: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def _upload_screenshot_to_cp(
    filename: str,
    data: bytes,
    caption: str = "Screenshot",
    metadata: Optional[dict[str, Any]] = None,
) -> tuple[str, str]:
    """Uploads PNG bytes to Control Panel if run credentials are configured, returning (url, markdown)."""
    cp_url = (os.environ.get("CONTROL_PANEL_URL") or os.environ.get("CP_URL") or "").strip().rstrip("/")
    run_id = (os.environ.get("FLOW_RUN_ID") or "").strip()
    run_cred = (os.environ.get("FLOW_RUN_CREDENTIAL") or "").strip()
    if not (cp_url and run_id and run_cred):
        return "", ""
    try:
        headers = {"Authorization": f"Bearer {run_cred}"}
        inst_id = os.environ.get("INSTANCE_ID", "").strip()
        if inst_id:
            headers["X-Instance-ID"] = inst_id
        payload: dict[str, Any] = {
            "filename": filename,
            "data_base64": base64.b64encode(data).decode("ascii"),
            "caption": caption,
        }
        if metadata:
            payload["metadata"] = metadata
        resp = httpx.post(
            f"{cp_url}/v1/runs/{run_id}/screenshots",
            headers=headers,
            json=payload,
            timeout=15.0,
        )
        if resp.status_code in (200, 201):
            body = resp.json()
            return str(body.get("url", "")), str(body.get("markdown", ""))
    except Exception:
        pass
    return "", ""


def load_recorded_screenshots(
    workspace_dir: Optional[Union[str, Path]] = None,
) -> list[dict[str, Any]]:
    """Loads screenshots captured during the current run whose URL is strictly hosted on CP_PUBLIC_URL."""
    cp_base = (
        os.environ.get("CP_PUBLIC_URL")
        or os.environ.get("CONTROL_PANEL_URL")
        or os.environ.get("CP_URL")
        or ""
    ).strip().rstrip("/")
    if not cp_base:
        return []
    url_re = re.compile(
        re.escape(f"{cp_base}/v1/runs/")
        + r"[A-Za-z0-9_.-]+/screenshots/[A-Za-z0-9_.-]+(?:\?exp=\d+&sig=[0-9a-f]{64})?$"
    )
    if workspace_dir:
        manifest_path = Path(workspace_dir).expanduser().resolve() / DEFAULT_ARTIFACTS_DIR / "screenshots.json"
    else:
        manifest_path = resolve_default_artifacts_dir() / "screenshots.json"
    if not manifest_path.is_file():
        return []
    try:
        items = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(items, list):
            out: list[dict[str, Any]] = []
            for x in items:
                if not isinstance(x, dict):
                    continue
                url = str(x.get("url") or "").strip()
                if url_re.fullmatch(url):
                    item_copy: dict[str, Any] = {**x, "url": url, "markdown": f"![Screenshot]({url})"}
                    item_copy["provenance_markdown"] = render_screenshot_with_provenance(item_copy)
                    out.append(item_copy)
            has_valid_win = any(
                (
                    e.get("has_active_window")
                    if "has_active_window" in e
                    else (e.get("metadata") or {}).get("has_active_window", True)
                )
                is not False
                for e in out
            )
            if has_valid_win:
                out = [
                    e
                    for e in out
                    if (
                        e.get("has_active_window")
                        if "has_active_window" in e
                        else (e.get("metadata") or {}).get("has_active_window", True)
                    )
                    is not False
                ]
            return out
    except Exception:
        pass
    return []


def capture_screen_bytes() -> bytes:
    """Captures native X11 framebuffer for DISPLAY=:0, returning raw PNG bytes."""
    display = os.environ.get("DISPLAY", ":0") or ":0"
    try:
        img = ImageGrab.grab(xdisplay=display)
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        return buffered.getvalue()
    except Exception as e:
        raise RuntimeError(f"Failed to capture visual screen: {e}") from e


def record_visual_artifact(
    data: bytes,
    format: str = "png",
    artifacts_dir: Optional[Union[str, Path]] = None,
    caption: str = "Screenshot",
    *,
    data_mode: str = "undeclared",
    data_source: str = "",
    constraints: str = "",
    scenario: str = "",
    display_provenance: Optional[dict[str, Any]] = None,
) -> VisualArtifact:
    """Saves raw image bytes to the artifacts directory and uploads to the Control Panel if configured."""
    dir_path = Path(artifacts_dir) if artifacts_dir else resolve_default_artifacts_dir()
    dir_path.mkdir(parents=True, exist_ok=True)

    digest_hex = hashlib.sha256(data).hexdigest()
    digest = f"sha256:{digest_hex}"
    filename = f"{digest_hex[:16]}.{format}"
    file_path = dir_path / filename
    # Also write full hex filename for backwards-compatibility with any callers checking <sha256>.png
    full_hex_path = dir_path / f"{digest_hex}.{format}"
    file_path.write_bytes(data)
    if full_hex_path != file_path:
        try:
            full_hex_path.write_bytes(data)
        except OSError:
            pass

    norm_mode = str(data_mode or "undeclared").strip().lower()
    if norm_mode not in _VALID_DATA_MODES:
        norm_mode = "undeclared"
    os_prov = (
        dict(display_provenance)
        if isinstance(display_provenance, dict)
        else _collect_display_provenance(os.environ.get("DISPLAY", ":0") or ":0", data=data)
    )
    if not os_prov.get("resolution"):
        os_prov["resolution"] = _extract_png_resolution(data)

    metadata: dict[str, Any] = {
        "data_mode": norm_mode,
        "data_source": str(data_source or "")[:500],
        "constraints": str(constraints or "")[:500],
        "scenario": str(scenario or "")[:500],
        "x11_windows": list(os_prov.get("x11_windows") or [])[:10],
        "active_processes": [_redact_command_secrets(str(p))[:500] for p in list(os_prov.get("active_processes") or [])[:10]],
        "has_active_window": bool(os_prov.get("has_active_window", True)),
        "resolution": str(os_prov.get("resolution") or ""),
        "captured_at": str(os_prov.get("captured_at") or datetime.now(timezone.utc).isoformat()),
    }

    url, markdown = _upload_screenshot_to_cp(filename, data, caption=caption, metadata=metadata)
    resolved_path = str(full_hex_path.resolve())
    evidence = url or f"artifact:{digest}"
    if not markdown and url:
        markdown = f"![{caption}]({url})"

    # Append to screenshots.json manifest in the artifacts dir (and mirror to workspace_dir() default)
    entry: dict[str, Any] = {
        "path": resolved_path,
        "sha256": digest,
        "evidence": evidence,
        "url": url,
        "markdown": markdown,
        "size_bytes": len(data),
        "run_id": os.environ.get("FLOW_RUN_ID", ""),
        **metadata,
        "metadata": metadata,
    }
    target_manifests = [dir_path / "screenshots.json"]
    try:
        from flow_agent.tools.git_tools import workspace_dir

        ws_default_dir = Path(workspace_dir()).expanduser().resolve() / DEFAULT_ARTIFACTS_DIR
        if ws_default_dir.resolve() != dir_path.resolve():
            ws_default_dir.mkdir(parents=True, exist_ok=True)
            target_manifests.append(ws_default_dir / "screenshots.json")
    except Exception:
        pass

    for manifest_path in target_manifests:
        try:
            existing: list[dict[str, Any]] = []
            if manifest_path.is_file():
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    existing = [x for x in loaded if isinstance(x, dict)]
            if not any(x.get("sha256") == digest for x in existing):
                existing.append(entry)
                manifest_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        except Exception:
            pass

    return VisualArtifact(
        sha256=digest,
        evidence=evidence,
        path=resolved_path,
        size_bytes=len(data),
        url=url,
        markdown=markdown,
        metadata=metadata,
    )


def capture_evaluator_screenshot(
    data_mode: DataMode = "undeclared",
    data_source: str = "",
    constraints: str = "",
    scenario: str = "",
) -> Dict[str, Any]:
    """Captures a UI screenshot, saves it in the workspace, and uploads it with provenance for Issue/PR visibility."""
    data = capture_screen_bytes()
    artifact = record_visual_artifact(
        data,
        caption="UI Screenshot",
        data_mode=data_mode,
        data_source=data_source,
        constraints=constraints,
        scenario=scenario,
    )
    res: Dict[str, Any] = {
        "sha256": artifact.sha256,
        "evidence": artifact.evidence,
        "path": artifact.path,
        "url": artifact.url,
        "markdown": artifact.markdown,
        "status": "recorded",
        "metadata": artifact.metadata,
    }
    if artifact.metadata.get("has_active_window") is False:
        res["warning"] = (
            "No active X11 application window detected on DISPLAY=:0 — "
            "verify your background UI process is still running"
        )
    return res


def capture_screen() -> str:
    """
    Captures the native X11 framebuffer for DISPLAY=:0.
    Returns a base64 encoded PNG string.
    """
    try:
        # Pillow >= 7.1.0 supports xdisplay argument on Linux
        img = ImageGrab.grab(xdisplay=":0")
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        return base64.b64encode(buffered.getvalue()).decode("utf-8")
    except Exception as e:
        raise RuntimeError(f"Failed to capture screen: {e}")


async def get_annotated_dom() -> str:
    """
    Connects to local Chrome CDP, extracts the DOM, strips heavy elements,
    and injects data-flow-id attributes into interactive elements.
    """
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get("http://localhost:29229/json")
            resp.raise_for_status()
            targets = resp.json()
            if not targets:
                raise RuntimeError("No Chrome targets found via CDP.")
            # Prefer 'page' target
            page_target = next((t for t in targets if t.get("type") == "page"), targets[0])
            ws_url = page_target.get("webSocketDebuggerUrl")
            if not ws_url:
                raise RuntimeError("No webSocketDebuggerUrl in target.")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to connect to Chrome CDP endpoint: {e}")

    async with websockets.connect(ws_url) as ws:
        # Request DOM.getDocument to initialize DOM agent and get root
        await ws.send(json.dumps({
            "id": 1,
            "method": "DOM.getDocument",
            "params": {"depth": -1}
        }))

        # Wait for the response to our command (id: 1)
        # CDP can send events concurrently, so we iterate until we find our response
        root_node_id = None
        while True:
            resp_str = await ws.recv()
            resp_data = json.loads(resp_str)
            if resp_data.get("id") == 1:
                if "error" in resp_data:
                    raise RuntimeError(f"CDP DOM.getDocument failed: {resp_data['error']}")
                root_node_id = resp_data["result"]["root"]["nodeId"]
                break

        # Get outer HTML of the root node
        await ws.send(json.dumps({
            "id": 2,
            "method": "DOM.getOuterHTML",
            "params": {"nodeId": root_node_id}
        }))

        outer_html = None
        while True:
            resp_str = await ws.recv()
            resp_data = json.loads(resp_str)
            if resp_data.get("id") == 2:
                outer_html = resp_data["result"]["outerHTML"]
                break

    # Parse and strip heavy elements
    soup = BeautifulSoup(outer_html, "html.parser")
    for tag in soup(["script", "style", "svg"]):
        tag.decompose()

    # Inject data-flow-id for interactive elements
    interactive_tags = ["button", "input", "a", "select", "textarea"]
    flow_id = 1
    for tag in soup.find_all(interactive_tags):
        tag["data-flow-id"] = str(flow_id)
        flow_id += 1

    return wrap_untrusted(str(soup), source="ui_dom")


async def inspect_ui(
    data_mode: DataMode = "undeclared",
    data_source: str = "",
    constraints: str = "",
    scenario: str = "",
) -> Dict[str, Any]:
    """
    Captures the GUI framebuffer on DISPLAY=:0, persists the PNG inside the
    workspace (.atobar/artifacts/) for multimodal inspection via read_file(path),
    uploads it with provenance to the Control Panel so it renders in the Issue & PR,
    and extracts the annotated Chrome DOM if a browser is open.
    """
    raw_bytes = capture_screen_bytes()
    artifact = record_visual_artifact(
        raw_bytes,
        caption="UI Inspection Screenshot",
        data_mode=data_mode,
        data_source=data_source,
        constraints=constraints,
        scenario=scenario,
    )

    annotated_dom = ""
    try:
        annotated_dom = await get_annotated_dom()
    except Exception:
        pass

    res: Dict[str, Any] = {
        "status": "recorded",
        "path": artifact.path,
        "url": artifact.url,
        "markdown": artifact.markdown,
        "sha256": artifact.sha256,
        "evidence": artifact.evidence,
        "annotated_dom": annotated_dom,
        "metadata": artifact.metadata,
        "hint": "Call view_image on 'path' to view the screenshot image directly, and include 'markdown' in your PR description.",
    }
    if artifact.metadata.get("has_active_window") is False:
        res["warning"] = (
            "No active X11 application window detected on DISPLAY=:0 — "
            "verify your background UI process is still running"
        )
    return res
