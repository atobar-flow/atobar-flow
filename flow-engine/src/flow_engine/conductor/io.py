"""Conductor IO boundary: load PR state, apply pure step(), and record loop rounds."""

import argparse
import json
import os
import secrets
import sys

from atobar_contracts.conductor_io import (
    _parse_ts,
    _run_serverless_arbitrator,
    apply_actions,
    compute_event_id,
    linked_prs_for_issue,
    parse_human_comment_directive,
    process_flow_event,
    rebuild_loop_state,
    resolve_delegated_issue_number,
)
from atobar_contracts.constants import LOOP_EPOCH_MARKER, get_bot_logins
from atobar_contracts.github_rest import GitHubClient, RealGitHubClient
from flow_engine.store import Store, get_store

__all__ = [
    "GitHubClient",
    "LOOP_EPOCH_MARKER",
    "RealGitHubClient",
    "Store",
    "_parse_ts",
    "_run_serverless_arbitrator",
    "apply_actions",
    "compute_event_id",
    "get_bot_logins",
    "get_store",
    "linked_prs_for_issue",
    "main",
    "parse_human_comment_directive",
    "process_flow_event",
    "rebuild_loop_state",
    "resolve_delegated_issue_number",
    "secrets",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Conductor step for flow_event")
    parser.add_argument("--payload-json", default="", help="JSON string of flow_event client_payload")
    args = parser.parse_args()

    raw_payload_str = args.payload_json or os.environ.get("CLIENT_PAYLOAD", "")
    if not raw_payload_str:
        print("Error: No client payload provided via --payload-json or CLIENT_PAYLOAD", file=sys.stderr)
        sys.exit(1)

    try:
        payload = json.loads(raw_payload_str)
    except Exception as e:
        print(f"Error parsing client payload JSON: {e}", file=sys.stderr)
        sys.exit(1)

    gh_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not gh_token:
        print("Error: Missing GITHUB_TOKEN environment variable", file=sys.stderr)
        sys.exit(1)

    gh = RealGitHubClient(token=gh_token)
    store = get_store()
    bot_logins = get_bot_logins()

    res = process_flow_event(payload=payload, gh=gh, store=store, bot_logins=bot_logins)
    print(f"Conductor result: {res}")


if __name__ == "__main__":
    main()
