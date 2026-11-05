import os
import re
import subprocess
import time
import uuid

import httpx

from flow_agent.core.config import get_flow_config
from flow_agent.core.untrusted import wrap_untrusted
from flow_agent.tools.fs_tools import register_overflow_path

_JOB_ID_RE = re.compile(r"^\d{1,20}$")


def get_truncation_limit() -> int:
    config = get_flow_config()
    return int(config.get("max_stdout_truncation_chars", 4000))


def _get_github_token() -> str:
    return os.environ.get("GITHUB_TOKEN", "")


def _get_repo_info():
    """Returns (owner, repo, ref)"""
    try:
        url = subprocess.check_output(["git", "config", "--get", "remote.origin.url"]).decode().strip()
        if "github.com/" in url:
            path = url.split("github.com/")[1]
        elif "github.com:" in url:
            path = url.split("github.com:")[1]
        else:
            path = "unknown/unknown"

        if path.endswith(".git"):
            path = path[:-4]

        owner, repo = path.split("/")
        ref = subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip()
        return owner, repo, ref
    except Exception:
        return "owner", "repo", "main"


def git_pr_checks(wait_mode: str = "all") -> str:
    """
    Polls the GitHub REST API for check runs on the current commit.
    wait_mode="all": Block until all checks complete.
    wait_mode="failure": Block until a failure or all complete.
    wait_mode="none": Return instantly.
    """
    token = _get_github_token()
    owner, repo, ref = _get_repo_info()

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{ref}/check-runs"

    with httpx.Client(headers=headers) as client:
        while True:
            resp = client.get(url)
            if resp.status_code != 200:
                return f"Error fetching checks: {resp.status_code} {resp.text}"

            data = resp.json()
            check_runs = data.get("check_runs", [])

            if not check_runs:
                if wait_mode == "none":
                    return "No check runs found for this commit."
                time.sleep(15)
                continue

            all_completed = True
            any_failure = False

            status_lines = []

            for run in check_runs:
                status = run.get("status")
                conclusion = run.get("conclusion")
                name = run.get("name", "unknown")
                job_id = run.get("id")

                if status != "completed":
                    all_completed = False
                elif conclusion in ("failure", "timed_out", "action_required"):
                    any_failure = True

                status_lines.append(f"- {name} (ID: {job_id}): {status} / {conclusion}")

            formatted_status = "\n".join(status_lines)

            if wait_mode == "none":
                return wrap_untrusted(formatted_status, source="ci_logs")

            if wait_mode == "failure" and any_failure:
                return wrap_untrusted(f"Checks failed:\n{formatted_status}", source="ci_logs")

            if all_completed:
                return wrap_untrusted(f"All checks completed:\n{formatted_status}", source="ci_logs")

            time.sleep(15)


def git_ci_job_logs(job_id: str) -> str:
    """
    Fetches logs for a specific failed job via the GitHub API and applies head-truncation.
    """
    job_id_str = str(job_id or "").strip()
    if not _JOB_ID_RE.fullmatch(job_id_str):
        return f"Invalid job_id {job_id!r}: must be 1 to 20 digits."

    token = _get_github_token()
    owner, repo, _ = _get_repo_info()

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"https://api.github.com/repos/{owner}/{repo}/actions/jobs/{job_id_str}/logs"

    with httpx.Client(headers=headers, follow_redirects=True) as client:
        resp = client.get(url)
        if resp.status_code != 200:
            return f"Error fetching logs for job {job_id_str}: {resp.status_code} {resp.text}"

        log_text = resp.text

    limit = get_truncation_limit()

    if len(log_text) > limit:
        truncated = log_text[:limit]
        file_uuid = uuid.uuid4().hex
        overflow_path = register_overflow_path(f"/tmp/overflow_job_{job_id_str}_{file_uuid}.log")

        with open(overflow_path, "w") as f:
            f.write(log_text)

        notice = (
            f'\n\n<truncation_notice path="{overflow_path}">'
            'Output truncated. Use the ripgrep tool on this file to locate errors.'
            '</truncation_notice>'
        )
        return wrap_untrusted(truncated + notice, source="ci_logs")

    return wrap_untrusted(log_text, source="ci_logs")
