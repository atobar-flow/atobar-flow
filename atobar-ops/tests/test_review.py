"""Tests for `atobar-ops review --wait` and review-round counter (J47 / #359 WP-2)."""

from __future__ import annotations

import argparse
import json
import subprocess
from typing import Any

import pytest

from atobar_ops import cli


def _args(**overrides: Any) -> argparse.Namespace:
    base = {
        "pr": 359,
        "repo": "atobar-flow/atobar-flow",
        "unresolved": False,
        "wait": False,
        "until_reviewer": None,
        "timeout": 60,
        "json": False,
        "verbose": False,
        "max_chars": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _gql_payload(
    *,
    head_oid: str = "sha2",
    pr_author: str = "arsenyspb",
    reviews: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
    threads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "number": 359,
                    "title": "feat(ops): ergonomics overhaul",
                    "state": "OPEN",
                    "url": "https://github.com/atobar-flow/atobar-flow/pull/359",
                    "headRefOid": head_oid,
                    "author": {"login": pr_author},
                    "reviews": {"nodes": reviews or []},
                    "comments": {"nodes": comments or []},
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": threads or [],
                    },
                }
            }
        }
    }


def test_wp2_review_round_counter_prints_stop_hint_on_round_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _gql_payload(
        head_oid="sha2",
        reviews=[
            {
                "author": {"login": "devin-ai-integration[bot]"},
                "state": "COMMENTED",
                "submittedAt": "2026-10-25T10:05:00Z",
                "body": "First review round",
                "commit": {"oid": "sha1"},
            },
            {
                "author": {"login": "devin-ai-integration[bot]"},
                "state": "COMMENTED",
                "submittedAt": "2026-10-25T10:15:00Z",
                "body": "Second review round after fix",
                "commit": {"oid": "sha2"},
            },
        ],
    )
    monkeypatch.setattr(
        cli.gh,
        "run",
        lambda args, *, timeout=30: subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr=""),
    )
    rc = cli.cmd_review(_args())
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "ROUND 2 — you already pushed a fix after review 1; STOP and report unless the user asked for another round" in out


def test_wp2_review_wait_ignores_stale_commit_review_even_with_late_timestamp(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    polls = {"n": 0}
    monkeypatch.setattr(cli.timebox.Deadline, "sleep", lambda self, s: None)

    # Poll 1: review submitted AFTER commit creation time, but on old sha1 -> must NOT match!
    # Also has PR author's own comment and terraform-cloud comment after push -> must NOT match!
    stale_on_sha1 = _gql_payload(
        head_oid="sha2",
        pr_author="arsenyspb",
        reviews=[
            {
                "author": {"login": "devin-ai-integration[bot]"},
                "state": "COMMENTED",
                "submittedAt": "2026-10-25T10:59:00Z",
                "body": "Late review on previous sha1",
                "commit": {"oid": "sha1"},
            }
        ],
        comments=[
            {"author": {"login": "arsenyspb"}, "body": "My own comment", "createdAt": "2026-10-25T11:05:00Z"},
            {"author": {"login": "terraform-cloud"}, "body": "Plan ok", "createdAt": "2026-10-25T11:05:00Z"},
        ],
    )
    fresh_on_sha2 = _gql_payload(
        head_oid="sha2",
        pr_author="arsenyspb",
        reviews=[
            {
                "author": {"login": "devin-ai-integration[bot]"},
                "state": "APPROVED",
                "submittedAt": "2026-10-25T11:06:00Z",
                "body": "Fresh review on sha2",
                "commit": {"oid": "sha2"},
            }
        ],
    )

    def fake_gh_run(args: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        if args == ["api", "user"]:
            return subprocess.CompletedProcess(args, 0, stdout='{"login":"arsenyspb"}', stderr="")
        if "check-suites" in args[-1]:
            return subprocess.CompletedProcess(
                args, 0, stdout='{"check_suites":[{"created_at":"2026-10-25T11:00:00Z"}]}', stderr=""
            )
        polls["n"] += 1
        data = stale_on_sha1 if polls["n"] == 1 else fresh_on_sha2
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(data), stderr="")

    monkeypatch.setattr(cli.gh, "run", fake_gh_run)
    args = _args(wait=True, until_reviewer=None, timeout=280)
    rc = cli.cmd_review(args)
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert polls["n"] == 2
    assert args.verbose is True
    assert "Fresh review on sha2" in out


def test_wp2_review_wait_timeout_returns_exit_timeout_and_next_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = _gql_payload(head_oid="sha2", reviews=[])
    monkeypatch.setattr(
        cli.gh,
        "run",
        lambda args, *, timeout=30: subprocess.CompletedProcess(args, 0, stdout=json.dumps(stale), stderr=""),
    )
    monkeypatch.setattr(cli.timebox.Deadline, "expired", lambda self: True)
    rc = cli.cmd_review(_args(wait=True, until_reviewer="devin", timeout=30))
    out = capsys.readouterr().out
    assert rc == cli.EXIT_TIMEOUT
    assert "NEXT: re-run once with --wait" in out
