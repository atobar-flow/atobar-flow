from unittest.mock import MagicMock, patch

from flow_engine.conductor.io import RealGitHubClient


def _resp(data):
    r = MagicMock()
    r.json.return_value = data
    return r


def test_resolves_open_pr_whose_head_is_the_sha():
    pulls = [
        {"number": 40, "state": "closed", "head": {"sha": "abc"}},
        {"number": 42, "state": "open", "head": {"sha": "abc"}},
    ]
    with patch("atobar_contracts.github_rest.httpx.get", return_value=_resp(pulls)) as get:
        assert RealGitHubClient("t").find_pr_for_sha("o/r", "abc") == 42
    assert get.call_args.args[0].endswith("/repos/o/r/commits/abc/pulls")


def test_no_open_pr_for_sha():
    pulls = [{"number": 41, "state": "open", "head": {"sha": "other"}}]
    with patch("atobar_contracts.github_rest.httpx.get", return_value=_resp(pulls)):
        assert RealGitHubClient("t").find_pr_for_sha("o/r", "abc") is None
