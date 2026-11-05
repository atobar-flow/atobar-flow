"""Repository-root pytest configuration (loaded for every package test suite)."""

from __future__ import annotations

import os

import pytest

GIT_QUIET_INIT_ENV: dict[str, str] = {
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "advice.defaultBranchName",
    "GIT_CONFIG_VALUE_0": "false",
    "GIT_CONFIG_KEY_1": "init.defaultBranch",
    "GIT_CONFIG_VALUE_1": "main",
}


def pytest_configure(config: pytest.Config) -> None:
    del config
    if "GIT_CONFIG_COUNT" not in os.environ:
        os.environ.update(GIT_QUIET_INIT_ENV)
