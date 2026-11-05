import os
from unittest.mock import MagicMock, patch

from flow_agent.tools.search_tools import search_codebase


@patch("subprocess.run")
@patch("flow_agent.tools.search_tools.get_truncation_limit", return_value=10)
def test_search_codebase_truncation(mock_limit, mock_subprocess):
    mock_result = MagicMock()
    mock_result.stdout = "This is a very long string that should definitely be truncated."
    mock_result.stderr = ""
    mock_result.returncode = 0
    mock_subprocess.return_value = mock_result

    result = search_codebase("long")

    assert "This is a " in result
    assert "<truncation_notice path=\"/tmp/overflow_rg_" in result

    for f in os.listdir("/tmp"):
        if f.startswith("overflow_rg_") and f.endswith(".log"):
            os.remove(os.path.join("/tmp", f))

@patch("subprocess.run")
def test_search_codebase_no_match(mock_subprocess):
    mock_result = MagicMock()
    mock_result.stdout = ""
    mock_result.stderr = ""
    mock_result.returncode = 1
    mock_subprocess.return_value = mock_result

    assert search_codebase("notfound") == "No matches found."

@patch("subprocess.run")
def test_search_codebase_error(mock_subprocess):
    mock_result = MagicMock()
    mock_result.stdout = ""
    mock_result.stderr = "Permission denied"
    mock_result.returncode = 2
    mock_subprocess.return_value = mock_result

    assert "STDERR:\nPermission denied" in search_codebase("error")
