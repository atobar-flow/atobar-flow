from unittest.mock import MagicMock, patch

from flow_agent.tools import git_tools


def _fake_run(current_branch: str, origin_head: str = "refs/remotes/origin/main",
              gh_returncode: int = 0, gh_stderr: str = ""):
    """Build a subprocess.run side_effect that answers git/gh by subcommand."""
    def _run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.returncode = 0
        cp.stdout = ""
        cp.stderr = ""
        prog = cmd[0]
        sub = cmd[1] if len(cmd) > 1 else ""
        if prog == "git":
            if sub == "symbolic-ref":
                cp.stdout = origin_head + "\n"
            elif sub == "branch":
                cp.stdout = current_branch + "\n"
        elif prog == "gh":
            cp.returncode = gh_returncode
            cp.stderr = gh_stderr
            cp.stdout = "https://github.com/dummy-org/dummy-repo/pull/42\n"
        return cp
    return _run


def _gh_cmd(mock_run):
    for c in mock_run.call_args_list:
        if c.args[0][0] == "gh":
            return c.args[0]
    return None


def _git_cmds(mock_run, sub):
    return [c.args[0] for c in mock_run.call_args_list
            if c.args[0][0] == "git" and c.args[0][1] == sub]


def test_pr_created_on_base_branch_relocates_to_feature(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ISSUE_ID", "16")
    with patch.object(git_tools.subprocess, "run",
                      side_effect=_fake_run(current_branch="main")) as mock_run, \
         patch.object(git_tools, "git_pr_checks", return_value="All checks completed"):
        result = git_tools.create_and_verify_pr("title", "body")

    # A feature branch was created off the base branch
    checkouts = _git_cmds(mock_run, "checkout")
    assert checkouts and "flow/issue-16" in checkouts[0]

    # We pushed the feature branch, never the base branch
    pushes = _git_cmds(mock_run, "push")
    assert pushes and "flow/issue-16" in pushes[0]

    # PR opened with explicit base=main, head=feature
    gh = _gh_cmd(mock_run)
    assert gh is not None
    assert gh[gh.index("--base") + 1] == "main"
    assert gh[gh.index("--head") + 1] == "flow/issue-16"
    assert "pull/42" in result
    assert "All checks completed" in result


def test_pr_uses_existing_feature_branch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ISSUE_ID", "16")
    with patch.object(git_tools.subprocess, "run",
                      side_effect=_fake_run(current_branch="wip-branch")) as mock_run, \
         patch.object(git_tools, "git_pr_checks", return_value="ok"):
        git_tools.create_and_verify_pr("t", "b")

    # Already on a non-base branch -> no relocation checkout
    assert _git_cmds(mock_run, "checkout") == []
    gh = _gh_cmd(mock_run)
    assert gh[gh.index("--head") + 1] == "wip-branch"
    assert gh[gh.index("--base") + 1] == "main"


def test_pr_already_exists_is_handled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ISSUE_ID", "16")
    with patch.object(git_tools.subprocess, "run",
                      side_effect=_fake_run(current_branch="main", gh_returncode=1,
                                            gh_stderr="a pull request already exists")), \
         patch.object(git_tools, "git_pr_checks", return_value="ok"):
        result = git_tools.create_and_verify_pr("t", "b")
    assert "already exists" in result


def test_feature_branch_name_falls_back_without_issue_id(monkeypatch):
    monkeypatch.delenv("ISSUE_ID", raising=False)
    assert git_tools._feature_branch_name() == "flow/agent-changes"
    monkeypatch.setenv("ISSUE_ID", "7")
    assert git_tools._feature_branch_name() == "flow/issue-7"


def test_pr_prepends_fixes_issue_id_when_omitted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ISSUE_ID", "42")
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    monkeypatch.setenv("TARGET_REPO", "owner/repo")

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"html_url": "https://github.com/owner/repo/pull/100"}

    with patch.object(git_tools.subprocess, "run",
                      side_effect=_fake_run(current_branch="main")), \
         patch("httpx.Client.post", return_value=mock_resp) as mock_post, \
         patch.object(git_tools, "git_pr_checks", return_value="ok"):
        git_tools.create_and_verify_pr("Title", "Some description")

    posted_body = mock_post.call_args[1]["json"]["body"]
    assert posted_body.startswith("Fixes #42\n\nSome description")


def test_pr_preserves_existing_fixes_tag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ISSUE_ID", "42")
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    monkeypatch.setenv("TARGET_REPO", "owner/repo")

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"html_url": "https://github.com/owner/repo/pull/101"}

    with patch.object(git_tools.subprocess, "run",
                      side_effect=_fake_run(current_branch="main")), \
         patch("httpx.Client.post", return_value=mock_resp) as mock_post, \
         patch.object(git_tools, "git_pr_checks", return_value="ok"):
        git_tools.create_and_verify_pr("Title", "Closes #42\n\nSome description")

    posted_body = mock_post.call_args[1]["json"]["body"]
    assert posted_body == "Closes #42\n\nSome description"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ISSUE_ID", "16")
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    monkeypatch.setenv("TARGET_REPO", "owner/repo")

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"html_url": "https://github.com/owner/repo/pull/99"}

    with patch.object(git_tools.subprocess, "run",
                      side_effect=_fake_run(current_branch="main")) as mock_run, \
         patch("httpx.Client.post", return_value=mock_resp) as mock_post, \
         patch.object(git_tools, "git_pr_checks", return_value="All checks passed"):
        result = git_tools.create_and_verify_pr("Title", "Body")

    # REST endpoint was invoked, not gh CLI
    assert mock_post.called
    assert _gh_cmd(mock_run) is None
    assert "pull/99" in result
    assert "All checks passed" in result

