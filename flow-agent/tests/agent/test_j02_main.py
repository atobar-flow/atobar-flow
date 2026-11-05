import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from flow_agent.runtime.sandbox import Workspace


@pytest.mark.asyncio
async def test_j02_ac3_main_exits_on_checkout_mismatch():
    from flow_agent.main import main

    # Mocking os.environ to set FLOW_HEAD_SHA and FLOW_ROLE
    env_vars = {
        "FLOW_ROLE": "evaluator",
        "FLOW_HEAD_SHA": "expected_sha",
        "TARGET_REPO": "real/repo",
        "TARGET_REF": "expected_sha",
        "ISSUE_ID": "42",
        "GITHUB_TOKEN": "token",
        "INSTANCE_ID": "i-1234"
    }

    with patch.dict(os.environ, env_vars, clear=True):
        # We need to mock prepare, _shutdown, SessionRegistry, GitHubTelemetryUI
        with patch("flow_agent.main.prepare") as mock_prepare, \
             patch("flow_agent.main._shutdown", new_callable=AsyncMock) as mock_shutdown, \
             patch("flow_agent.main.SessionRegistry", autospec=True), \
             patch("flow_agent.main.GitHubTelemetryUI", autospec=True):

            # Return a workspace with wrong commit_sha
            import pathlib
            mock_prepare.return_value = Workspace(
                repo="real/repo",
                ref="expected_sha",
                path=pathlib.Path("/tmp/workspace"),
                commit_sha="wrong_sha"
            )

            # Mock parse args
            with patch("argparse.ArgumentParser.parse_known_args", return_value=(MagicMock(role="evaluator"), [])):
                await main()

            mock_shutdown.assert_called_once()
            err_msg = mock_shutdown.call_args[0][0]
            assert "Checkout mismatch for evaluator" in err_msg
            assert "expected_sha" in err_msg
            assert "wrong_sha" in err_msg

