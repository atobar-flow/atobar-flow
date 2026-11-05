import os
import re
import subprocess
from typing import Tuple

import httpx

from flow_agent.tools.ci_tools import git_pr_checks

WORKSPACE_DIR = "/home/ubuntu/workspace/"
_LAST_WORKFLOW_HANDOFF_NOTE: str = ""


def _is_workflow_path(path: str) -> bool:
    norm = path.replace("\\", "/").strip()
    while norm.startswith("./") or norm.startswith("/"):
        norm = norm[2:] if norm.startswith("./") else norm[1:]
    norm_lower = norm.lower()
    return norm_lower == ".github/workflows" or norm_lower.startswith(".github/workflows/")


def consume_workflow_handoff_note() -> str:
    global _LAST_WORKFLOW_HANDOFF_NOTE
    note = _LAST_WORKFLOW_HANDOFF_NOTE
    _LAST_WORKFLOW_HANDOFF_NOTE = ""
    return note


def workspace_dir() -> str:
    """Client repo checkout directory on the sandbox (FLOW_WORKSPACE_DIR -> WORKSPACE_DIR -> CWD)."""
    flow_ws = os.environ.get("FLOW_WORKSPACE_DIR", "").strip()
    if flow_ws and os.path.exists(flow_ws):
        return flow_ws
    env_ws = os.environ.get("WORKSPACE_DIR", "").strip()
    if env_ws and os.path.exists(env_ws):
        return env_ws
    return WORKSPACE_DIR if os.path.exists(WORKSPACE_DIR) else os.getcwd()


_workspace_dir = workspace_dir


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _detect_base_branch(cwd: str) -> str:
    """The client repo's default branch, used as the PR base."""
    res = _git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd)
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip().rsplit("/", 1)[-1]
    return "main"


def _feature_branch_name() -> str:
    issue_id = os.environ.get("ISSUE_ID", "").strip()
    return f"flow/issue-{issue_id}" if issue_id.isdigit() else "flow/agent-changes"


def _get_repo_owner_and_name(cwd: str) -> Tuple[str, str]:
    """Extract owner and repo name from TARGET_REPO or git remote."""
    target_repo = os.environ.get("TARGET_REPO", "").strip()
    if "/" in target_repo:
        parts = target_repo.split("/", 1)
        return parts[0], parts[1]

    res = _git(["config", "--get", "remote.origin.url"], cwd)
    url = res.stdout.strip()
    if "github.com/" in url:
        path = url.split("github.com/")[1]
    elif "github.com:" in url:
        path = url.split("github.com:")[1]
    else:
        path = "owner/repo"

    if path.endswith(".git"):
        path = path[:-4]

    if "/" in path:
        parts = path.split("/", 1)
        return parts[0], parts[1]
    return "owner", "repo"


def _create_pr_api(
    owner: str,
    repo: str,
    title: str,
    body: str,
    base: str,
    head: str,
    token: str,
) -> Tuple[bool, str]:
    """Create a pull request using GitHub REST API."""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "title": title,
        "body": body,
        "base": base,
        "head": head,
    }

    try:
        with httpx.Client(headers=headers, timeout=30.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code in (200, 201):
                data = resp.json()
                return True, data.get("html_url", "")
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            err_msg = data.get("message", resp.text)
            for err in data.get("errors", []):
                if isinstance(err, dict) and "already exists" in err.get("message", "").lower():
                    _update_existing_pr_body(client, owner, repo, head, body)
                    return True, f"PR already exists for {head}"
            if "already exists" in err_msg.lower():
                _update_existing_pr_body(client, owner, repo, head, body)
                return True, f"PR already exists for {head}"
            return False, f"GitHub API error ({resp.status_code}): {err_msg}"
    except Exception as e:
        return False, f"GitHub API request failed: {e}"


def _screenshot_already_in_body(body: str, url: str) -> bool:
    """Returns True if `url` or its base `/v1/runs/.../screenshots/<file>` path is already present in `body`."""
    clean_url = url.strip()
    if not clean_url:
        return True
    base_url = clean_url.split("?", 1)[0]
    return clean_url in body or (bool(base_url) and base_url in body)


def _update_existing_pr_body(
    client: httpx.Client,
    owner: str,
    repo: str,
    head: str,
    body: str,
) -> None:
    """Updates the body of an existing open PR for `head` without overwriting a rich PR body with a fallback stub."""
    try:
        list_resp = client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            params={"head": f"{owner}:{head}", "state": "open"},
        )
        if list_resp.status_code == 200:
            prs = list_resp.json()
            if isinstance(prs, list) and prs:
                pr_num = prs[0].get("number")
                existing_body = str(prs[0].get("body") or "")
                if not pr_num:
                    return
                is_fallback_stub = "Automated implementation by atobar-flow developer" in body
                if is_fallback_stub and existing_body and "Automated implementation by atobar-flow developer" not in existing_body:
                    new_body = existing_body.rstrip()
                    if "## Screenshots" in body and "## Screenshots" not in new_body:
                        screenshots_part = body.split("## Screenshots", 1)[1]
                        if "\n\nWorkflow file changes need a human:" in screenshots_part:
                            screenshots_part = screenshots_part.split("\n\nWorkflow file changes need a human:", 1)[0]
                        kept_blocks: list[str] = []
                        for block in screenshots_part.strip().split("\n\n"):
                            b_stripped = block.strip()
                            if not b_stripped:
                                continue
                            m_url = re.search(r"!\[[^\]]*\]\(([^)\s]+)\)", b_stripped)
                            if m_url and _screenshot_already_in_body(new_body, m_url.group(1)):
                                continue
                            kept_blocks.append(b_stripped)
                        if kept_blocks:
                            new_body = f"{new_body}\n\n## Screenshots\n\n" + "\n\n".join(kept_blocks)
                    if _LAST_WORKFLOW_HANDOFF_NOTE and _LAST_WORKFLOW_HANDOFF_NOTE not in new_body:
                        new_body = f"{new_body}\n\n{_LAST_WORKFLOW_HANDOFF_NOTE}"
                    if new_body != existing_body.rstrip():
                        client.patch(
                            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_num}",
                            json={"body": new_body},
                        )
                    return
                client.patch(
                    f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_num}",
                    json={"body": body},
                )
    except Exception:
        pass


def create_and_verify_pr(title: str, body: str, wait_ci: bool = True) -> str:
    """
    Opens a Pull Request for the agent's work and waits for CI.

    The agent edits files on the workspace's default branch, so this moves the
    work onto a dedicated feature branch before pushing (we never push to the
    client's default branch) and opens the PR with an explicit base/head. It
    then synchronously polls CI so the agent can enter a fix loop on failure.
    """
    workspace_dir = _workspace_dir()

    base_branch = _detect_base_branch(workspace_dir)

    cur = _git(["branch", "--show-current"], workspace_dir)
    if cur.returncode != 0:
        return f"Failed to get current branch: {cur.stderr}"
    current_branch = cur.stdout.strip()

    # If we're sitting on the base branch (or detached HEAD), relocate the work
    # onto a feature branch so head != base and the PR is valid.
    if not current_branch or current_branch == base_branch:
        feature_branch = _feature_branch_name()
        co = _git(["checkout", "-B", feature_branch], workspace_dir)
        if co.returncode != 0:
            return f"Failed to create feature branch {feature_branch}: {co.stderr}"
        current_branch = feature_branch

    if current_branch == base_branch:
        return (
            f"Refusing to open a PR from the base branch '{base_branch}'. "
            "Commit the work on a feature branch first."
        )

    # Work the agent left uncommitted is still the implementation; never drop it,
    # but ensure internal agent telemetry / artifacts never leak into the client PR branch.
    global _LAST_WORKFLOW_HANDOFF_NOTE
    _LAST_WORKFLOW_HANDOFF_NOTE = ""
    strip_targets = [".atobar/state.json", ".atobar/trace.jsonl", ".atobar/artifacts"]
    _git(["add", "-A"], workspace_dir)
    _git(["rm", "-r", "--cached", "--ignore-unmatch", *strip_targets], workspace_dir)
    if _git(["diff", "--cached", "--quiet"], workspace_dir).returncode != 0:
        _git(["commit", "-m", title], workspace_dir)

    # J42 WP-2 (UJ-01): developer lacks workflows:write, so drop .github/workflows/** before pushing
    diff_names = _git(["diff", "--name-only", f"origin/{base_branch}..HEAD"], workspace_dir)
    changed_paths = [p.strip() for p in diff_names.stdout.splitlines() if p.strip()]
    workflow_paths = sorted(p for p in changed_paths if _is_workflow_path(p))
    if workflow_paths:
        _LAST_WORKFLOW_HANDOFF_NOTE = f"Workflow file changes need a human: {', '.join(workflow_paths)}"
        non_workflow_paths = [p for p in changed_paths if not _is_workflow_path(p)]
        if not non_workflow_paths:
            _git(["reset", "--hard", f"origin/{base_branch}"], workspace_dir)
            return f"Workflow-only changes skipped push ({_LAST_WORKFLOW_HANDOFF_NOTE})"
        # Restore .github/workflows/** to origin/{base_branch} (or remove if newly added)
        for wf_path in workflow_paths:
            co_res = _git(["checkout", f"origin/{base_branch}", "--", wf_path], workspace_dir)
            if co_res.returncode != 0:
                _git(["rm", "-f", "--ignore-unmatch", "--", wf_path], workspace_dir)
        _git(["commit", "--amend", "--no-edit"], workspace_dir)
        body = f"{body.rstrip()}\n\n{_LAST_WORKFLOW_HANDOFF_NOTE}"

    if _git(["rev-list", "--count", f"origin/{base_branch}..HEAD"], workspace_dir).stdout.strip() == "0":
        return f"Failed to create PR: no commits on {current_branch} beyond {base_branch}; nothing was implemented."

    # flow/* branches are factory-owned; a lease breaks when a previous PR's
    # branch was deleted or rewritten, so overwrite unconditionally.
    push = _git(
        ["push", "--force", "--set-upstream", "origin", current_branch],
        workspace_dir,
    )
    if push.returncode != 0:
        return f"Failed to push branch to origin: {push.stderr}"

    # Deterministic issue linking per Shift-Left Determinism (Axiom 5)
    issue_id = os.environ.get("ISSUE_ID", "").strip()
    if issue_id and not re.search(rf"\b(Fixes|Closes|Resolves)\s*#{issue_id}\b", body, re.IGNORECASE):
        body = f"Fixes #{issue_id}\n\n{body}"

    # Attach any screenshots captured during this run to the PR description
    try:
        from flow_agent.tools.vision_tools import load_recorded_screenshots

        screens = load_recorded_screenshots(workspace_dir)
        missing_md = []
        for s in screens:
            url = (s.get("url") or "").strip()
            if not url:
                continue
            md = f"![Screenshot]({url})"
            if not _screenshot_already_in_body(body, url) and md not in body:
                missing_md.append(md)
        if missing_md:
            body = f"{body.rstrip()}\n\n## Screenshots\n\n" + "\n\n".join(missing_md)
    except Exception:
        pass

    token = os.environ.get("GITHUB_TOKEN", "")
    owner, repo = _get_repo_owner_and_name(workspace_dir)

    # Preferred REST API path (removes undeclared CLI runtime dependency)
    pr_url = ""
    if token:
        success, api_output = _create_pr_api(
            owner=owner, repo=repo, title=title, body=body, base=base_branch, head=current_branch, token=token
        )
        if success:
            pr_url = api_output
        else:
            return f"Failed to create PR: {api_output}"
    else:
        # Fallback to gh CLI if GITHUB_TOKEN is not present
        body_file_path = os.path.join(workspace_dir, ".pr_body_tmp.md")
        try:
            with open(body_file_path, "w", encoding="utf-8") as f:
                f.write(body)

            pr_res = subprocess.run(
                [
                    "gh", "pr", "create",
                    "--title", title,
                    "--body-file", ".pr_body_tmp.md",
                    "--base", base_branch,
                    "--head", current_branch,
                ],
                cwd=workspace_dir, capture_output=True, text=True,
            )
            if pr_res.returncode != 0:
                if "already exists" in pr_res.stderr:
                    pr_url = f"PR already exists for {current_branch}"
                else:
                    return f"Failed to create PR: {pr_res.stderr}"
            else:
                pr_url = pr_res.stdout.strip()
        finally:
            if os.path.exists(body_file_path):
                os.remove(body_file_path)

    if not wait_ci:
        return f"PR Creation Output: {pr_url}"

    # Delegate to ci_tools for polling
    ci_status = git_pr_checks(wait_mode="all")

    return f"PR Creation Output: {pr_url}\n\nCI Polling Output:\n{ci_status}"
