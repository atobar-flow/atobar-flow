import base64
import pathlib
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest

from flow_engine.conductor.dispatch import dispatch_spot

VALID_SHA = "0123456789abcdef0123456789abcdef01234567"


# --- AC-2: Dry-run mode prints the run-instances arguments for each role ---

@pytest.mark.parametrize("role", ["developer", "evaluator", "arbitrator"])
def test_ac2_dry_run_args_per_role(role, capsys):
    result = dispatch_spot(
        role=role,
        tenant="arsenyspb/ollama-monitor",
        pr=42,
        head_sha=VALID_SHA,
        dry_run=True,
    )

    assert result["dry_run"] is True
    args = result["args"]

    # Role instance profile
    assert "Name=flow-spot-sandbox-profile" in " ".join(args)

    # Tags for Role, Tenant, Pr
    arg_str = " ".join(args)
    assert f"Key=Role,Value={role}" in arg_str
    assert "Key=Tenant,Value=arsenyspb/ollama-monitor" in arg_str
    assert "Key=Pr,Value=42" in arg_str

    # User data decoded contains all required exports
    user_data_idx = args.index("--user-data") + 1
    raw_user_data = base64.b64decode(args[user_data_idx]).decode("utf-8")
    assert f'export FLOW_ROLE="{role}"' in raw_user_data
    assert 'export FLOW_TENANT="arsenyspb/ollama-monitor"' in raw_user_data
    assert 'export FLOW_PR="42"' in raw_user_data
    assert f'export FLOW_HEAD_SHA="{VALID_SHA}"' in raw_user_data

    # Output was printed in dry-run
    captured = capsys.readouterr()
    assert "aws ec2 run-instances" in captured.out
    assert role in captured.out


def test_legacy_payload_defaults_to_developer():
    # Role omitted / empty defaults to developer
    result = dispatch_spot(
        role="",
        tenant="owner/repo",
        pr=10,
        dry_run=True,
    )
    args = result["args"]
    assert "Name=flow-spot-sandbox-profile" in " ".join(args)
    assert "Key=Role,Value=developer" in " ".join(args)


def test_dispatch_spot_executes_with_fake_aws():
    fake_runner = MagicMock()
    fake_runner.side_effect = [
        # describe-launch-template-versions output
        base64.b64encode(b"#!/bin/bash\necho boot").decode(),
        # run-instances output
        "i-0123456789abcdef0",
    ]

    res = dispatch_spot(
        role="evaluator",
        tenant="owner/repo",
        pr=5,
        head_sha=VALID_SHA,
        dry_run=False,
        runner=fake_runner,
    )

    assert res["instance_id"] == "i-0123456789abcdef0"
    assert fake_runner.call_count == 2


def test_j01_ac2_validate_labels_and_identifiers():
    import pytest

    from flow_engine.conductor.dispatch import dispatch_spot

    # Invalid labels
    invalid_labels = [
        "flow:onboard", "flow:metrics", "flow:approved",
        "flow:escalated", "flow:in-progress",
        "flow:in-review", "flow:shipped", "flow:needs-approval",
        "random-label" # Not flow:*
    ]
    for label in invalid_labels:
        with pytest.raises(ValueError, match="Invalid label"):
            dispatch_spot(role="developer", tenant="owner/repo", pr=1, label=label, dry_run=True)

    # Valid labels
    valid_labels = ["flow:feature", "flow:fix", "flow:docs", "flow:custom"]
    for label in valid_labels:
        # Should not raise
        dispatch_spot(role="developer", tenant="owner/repo", pr=1, label=label, dry_run=True)

    # Invalid identifiers
    invalid_tenants = ["../repo", "owner/..", "owner/repo/sub", "owner"]
    for tenant in invalid_tenants:
        with pytest.raises(ValueError, match="Invalid tenant"):
            dispatch_spot(role="developer", tenant=tenant, pr=1, label="flow:feature", dry_run=True)


def test_build_user_data_agent_runtime():
    ud_nanobot = dispatch_spot(
        role="developer",
        tenant="owner/repo",
        pr=1,
        agent_runtime="nanobot",
        dry_run=True,
    )
    user_data_idx = ud_nanobot["args"].index("--user-data") + 1
    raw_user_data = base64.b64decode(ud_nanobot["args"][user_data_idx]).decode("utf-8")
    assert 'export FLOW_AGENT_RUNTIME="nanobot"' in raw_user_data

    with pytest.raises(ValueError, match="Invalid agent_runtime"):
        dispatch_spot(
            role="developer",
            tenant="owner/repo",
            pr=1,
            agent_runtime="invalid_runtime",
            dry_run=True,
        )


def test_build_user_data_target_ref():
    from flow_engine.conductor.dispatch import build_user_data

    ud_eval = build_user_data("evaluator", "owner/repo", 1, head_sha="1234abcd")
    assert 'export TARGET_REF="1234abcd"' in ud_eval

    ud_dev = build_user_data("developer", "owner/repo", 1, head_sha="1234abcd")
    assert 'export TARGET_REF="1234abcd"' in ud_dev

    ud_fresh = build_user_data("developer", "owner/repo", 1)
    assert 'export TARGET_REF="main"' in ud_fresh
