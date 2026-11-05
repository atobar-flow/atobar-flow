"""CI-parity workspace verification (`atobar-ops verify`) shared with `ship`."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from . import output, timebox
from .timebox import EXIT_ERROR, EXIT_OK, EXIT_TIMEOUT

GITLEAKS_VERSION = "8.24.3"
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

GIT_QUIET_INIT_ENV: dict[str, str] = {
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "advice.defaultBranchName",
    "GIT_CONFIG_VALUE_0": "false",
    "GIT_CONFIG_KEY_1": "init.defaultBranch",
    "GIT_CONFIG_VALUE_1": "main",
}


def pytest_subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    if "GIT_CONFIG_COUNT" not in env:
        env.update(GIT_QUIET_INIT_ENV)
    return env

WORKSPACE_PACKAGES: tuple[str, ...] = (
    "contracts",
    "control-panel",
    "flow-engine",
    "flow-agent",
    "atobar-ops",
)


def gitleaks_base_argv(binary: str) -> list[str]:
    """Single source of truth for gitleaks git-scan flags shared by `verify` and `.github/workflows/ci.yml`."""
    argv = [binary, "git", "--no-banner", "--redact"]
    if Path(".gitleaks.toml").is_file():
        argv.extend(["--config", ".gitleaks.toml"])
    return argv


def _mypy_command() -> list[str]:
    return [
        "uv",
        "run",
        "--no-sync",
        "mypy",
        *[f"{pkg}/src" for pkg in WORKSPACE_PACKAGES],
        "--ignore-missing-imports",
    ]


def ci_test_commands() -> list[tuple[str, list[str], int]]:
    """Returns the ordered (step_name, argv, default_timeout_s) matching `.github/workflows/ci.yml` `test` job."""
    return [
        ("ruff", ["uv", "run", "--no-sync", "ruff", "check", "."], 60),
        ("mypy", _mypy_command(), 90),
        ("pytest", ["uv", "run", "--no-sync", "pytest", "-q", "--tb=short", "--durations=10"], 180),
        ("schema", ["uv", "run", "--no-sync", "python", "-m", "atobar_contracts.schema", "--check"], 30),
        ("manifest", ["uv", "run", "--no-sync", "python", "-m", "atobar_contracts.manifest"], 30),
    ]


def _changed_paths(deadline: timebox.Deadline) -> list[str]:
    paths: set[str] = set()
    for ref_args in (
        ["diff", "--name-only", "origin/main"],
        ["diff", "--name-only", "HEAD"],
        ["ls-files", "--others", "--exclude-standard"],
    ):
        res = timebox.run(["git", *ref_args], timeout=max(5.0, min(15.0, deadline.remaining())))
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                p = line.strip()
                if p:
                    paths.add(p)
    return sorted(paths)


def _changed_verify_steps(changed: list[str]) -> list[tuple[str, list[str], int]]:
    existing = [p for p in changed if Path(p).exists()]
    py_targets = [p for p in existing if p.endswith(".py")]
    ruff_cmd = ["uv", "run", "--no-sync", "ruff", "check", *(py_targets or ["."])]

    # Root tests/ holds the cross-service guards (boundaries, table ownership, shims), so it always runs;
    # contracts/ is imported by every package, so a change there fans out to all package suites.
    dirs: list[str] = ["tests"]
    files: list[str] = []
    for p in changed:
        if p.endswith(".py") and ("/tests/" in f"/{p}" or Path(p).name.startswith("test_")):
            if Path(p).exists():
                files.append(p)
            continue
        pkg = next((pkg for pkg in WORKSPACE_PACKAGES if p.startswith(f"{pkg}/")), None)
        for tpkg in WORKSPACE_PACKAGES if pkg == "contracts" else [pkg] if pkg else []:
            dirs.append(f"{tpkg}/tests")
    dirs = [d for d in dict.fromkeys(dirs) if Path(d).is_dir()]
    test_targets = dirs + [f for f in dict.fromkeys(files) if not any(f.startswith(f"{d}/") for d in dirs)]

    steps: list[tuple[str, list[str], int]] = [("ruff", ruff_cmd, 45)]
    has_src_py = any(
        p.endswith(".py") and any(p.startswith(f"{pkg}/src/") for pkg in WORKSPACE_PACKAGES) for p in changed
    )
    if has_src_py:
        steps.append(("mypy", _mypy_command(), 90))
    steps.append(("pytest", ["uv", "run", "--no-sync", "pytest", *test_targets, "-q", "--tb=short"], 120))
    return steps


_TOP_BANNER_RE = re.compile(r"^=+\s+(.+?)\s+=+$")
_TEST_BANNER_RE = re.compile(r"^_+\s+(.+?)\s+_+$")


def extract_pytest_failure_excerpt(stdout: str, *, max_lines: int = 60, per_test_max: int = 18) -> str:
    """Extracts a bounded failure excerpt (FAILURES/ERRORS with E-lines prioritized + short summary)."""
    raw_lines = [ln.rstrip() for ln in stdout.splitlines()]
    if not raw_lines:
        return ""

    section: str | None = None
    failure_blocks: list[list[str]] = []
    current_block: list[str] | None = None
    summary_lines: list[str] = []

    for ln in raw_lines:
        m_top = _TOP_BANNER_RE.match(ln)
        if m_top:
            title = m_top.group(1).strip().upper()
            if current_block:
                failure_blocks.append(current_block)
                current_block = None
            if title in ("FAILURES", "ERRORS"):
                section = "failures"
                continue
            if title == "SHORT TEST SUMMARY INFO":
                section = "summary"
                summary_lines.append(ln)
                continue
            section = "other"
            continue

        if section == "failures":
            if _TEST_BANNER_RE.match(ln):
                if current_block:
                    failure_blocks.append(current_block)
                current_block = [ln]
            elif current_block is not None:
                current_block.append(ln)
            elif ln.strip():
                current_block = [ln]
        elif section == "summary":
            if ln.strip():
                summary_lines.append(ln)

    if current_block:
        failure_blocks.append(current_block)

    # Always include final status line (e.g. "1 failed, 10 passed in 0.42s") if not already in summary_lines
    tail_nonempty = [ln for ln in raw_lines if ln.strip()]
    if tail_nonempty and (not summary_lines or tail_nonempty[-1] != summary_lines[-1]):
        summary_lines.append(tail_nonempty[-1])

    if not failure_blocks and len(summary_lines) <= 1:
        return "\n".join(raw_lines[-25:])

    excerpt: list[str] = []
    for block in failure_blocks:
        header = block[:1]
        body = block[1:]
        # Drop captured stdout/stderr subsections before cutting so assertion diffs survive
        trimmed_body: list[str] = []
        in_captured = False
        for bln in body:
            if bln.startswith("---") and ("Captured stdout" in bln or "Captured stderr" in bln or "Captured log" in bln):
                in_captured = True
                continue
            if not in_captured:
                trimmed_body.append(bln)
        source_body = trimmed_body or body
        if len(header) + len(source_body) <= per_test_max:
            excerpt.extend(header + source_body)
        else:
            e_lines = [bln for bln in source_body if bln.lstrip().startswith("E ")]
            loc_lines = [bln for bln in source_body if not bln.lstrip().startswith("E ") and bln.strip()]
            keep_e = e_lines[: max(4, per_test_max - 3)]
            keep_loc = loc_lines[: max(1, per_test_max - 1 - len(keep_e))]
            excerpt.extend(header + keep_loc + keep_e)

    summary_budget = min(15, max(4, len(summary_lines)))
    kept_summary = summary_lines[-summary_budget:]
    body_budget = max(5, max_lines - len(kept_summary))
    combined = [*excerpt[:body_budget], *kept_summary]
    return "\n".join(combined[:max_lines])


def run_verify(
    args: argparse.Namespace,
    *,
    changed: bool = False,
    require_gitleaks: bool = False,
) -> tuple[int, list[str], dict[str, Any]]:
    """Executes verification steps and returns `(exit_code, lines, structured_data)`.

    A missing `gitleaks` binary is an explicit SKIP for `verify`, but a failure when `require_gitleaks` (used by `ship`).
    """
    timeout = timebox.resolve_timeout(getattr(args, "timeout", None))
    deadline = timebox.Deadline(timeout)
    lines: list[str] = []
    step_results: list[dict[str, Any]] = []
    failed_steps: list[str] = []
    timed_out = False

    steps = _changed_verify_steps(_changed_paths(deadline)) if changed else ci_test_commands()

    for name, cmd, step_timeout in steps:
        if deadline.expired():
            lines.append(f"{name}: TIMEOUT (overall deadline {timeout}s exceeded)")
            step_results.append({"step": name, "status": "timeout", "returncode": 124})
            failed_steps.append(name)
            timed_out = True
            break
        eff_timeout = max(5.0, min(float(step_timeout), deadline.remaining()))
        if name == "pytest":
            try:
                res = timebox.run(cmd, timeout=eff_timeout, env=pytest_subprocess_env())
            except TypeError:
                res = timebox.run(cmd, timeout=eff_timeout)
        else:
            res = timebox.run(cmd, timeout=eff_timeout)
        lines.append(f"{name}: rc={res.returncode}")
        if name == "pytest" and res.returncode == 0 and res.stdout.strip():
            lines.append("\n".join(res.stdout.splitlines()[-20:]))
        if res.returncode != 0:
            if name == "pytest":
                if res.stdout.strip():
                    lines.append(extract_pytest_failure_excerpt(res.stdout))
                if res.stderr.strip():
                    lines.append("\n".join(res.stderr.splitlines()[-15:]))
            else:
                tail = "\n".join((res.stdout + "\n" + res.stderr).strip().splitlines()[-25:])
                if tail:
                    lines.append(tail)
            failed_steps.append(name)
            if res.returncode == 124:
                timed_out = True
        step_results.append(
            {
                "step": name,
                "status": "passed" if res.returncode == 0 else ("timeout" if res.returncode == 124 else "failed"),
                "returncode": res.returncode,
            }
        )

    if not changed:
        gitleaks_bin = shutil.which("gitleaks")
        if not gitleaks_bin and require_gitleaks:
            lines.append(
                "gitleaks: MISSING: gitleaks not installed (required to ship; "
                "install via https://github.com/gitleaks/gitleaks#installing)"
            )
            step_results.append({"step": "gitleaks", "status": "failed", "returncode": None})
            failed_steps.append("gitleaks")
        elif not gitleaks_bin:
            lines.append("gitleaks: SKIPPED: gitleaks not installed")
            step_results.append({"step": "gitleaks", "status": "skipped", "returncode": None})
        elif deadline.expired():
            lines.append(f"gitleaks: TIMEOUT (overall deadline {timeout}s exceeded)")
            step_results.append({"step": "gitleaks", "status": "timeout", "returncode": 124})
            failed_steps.append("gitleaks")
            timed_out = True
        else:
            gl_base = gitleaks_base_argv(gitleaks_bin)
            gl_res = timebox.run(
                [*gl_base, "--log-opts=origin/main..HEAD"],
                timeout=max(5.0, min(30.0, deadline.remaining())),
            )
            if gl_res.returncode == 0 and not deadline.expired():
                pre_res = timebox.run(
                    [*gl_base, "--pre-commit"],
                    timeout=max(5.0, min(30.0, deadline.remaining())),
                )
                if pre_res.returncode != 0:
                    gl_res = pre_res
            lines.append(f"gitleaks: rc={gl_res.returncode}")
            if not deadline.expired():
                ver_res = timebox.run(
                    [gitleaks_bin, "version"],
                    timeout=max(2.0, min(5.0, deadline.remaining())),
                )
                local_ver = (ver_res.stdout or "").strip().splitlines()[0].strip().lstrip("v") if ver_res.stdout else ""
                if ver_res.returncode == 0 and _SEMVER_RE.match(local_ver) and local_ver != GITLEAKS_VERSION:
                    lines.append(f"gitleaks: NOTE: local {local_ver} != CI {GITLEAKS_VERSION}")
            if gl_res.returncode != 0:
                gl_tail = "\n".join((gl_res.stdout + "\n" + gl_res.stderr).strip().splitlines()[-20:])
                if gl_tail:
                    lines.append(gl_tail)
                failed_steps.append("gitleaks")
                if gl_res.returncode == 124:
                    timed_out = True
            step_results.append(
                {
                    "step": "gitleaks",
                    "status": "passed" if gl_res.returncode == 0 else "failed",
                    "returncode": gl_res.returncode,
                }
            )

    if failed_steps:
        lines.insert(0, f"VERIFY FAILED: {', '.join(failed_steps)}")
        rc = EXIT_TIMEOUT if timed_out else EXIT_ERROR
    else:
        lines.insert(0, "VERIFY OK" + (" (changed-only)" if changed else ""))
        rc = EXIT_OK

    return rc, lines, {"ok": rc == EXIT_OK, "failed_steps": failed_steps, "steps": step_results}


def cmd_verify(args: argparse.Namespace) -> int:
    changed = bool(getattr(args, "changed", False))
    rc, lines, data = run_verify(args, changed=changed)
    output.emit(args, lines, data=data)
    return rc
