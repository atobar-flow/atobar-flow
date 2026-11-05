"""Spot sandbox IAM zero-secret lockdown guard (#301 WP-0 / AC-1)."""

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_spot_iam_denies_ssm_and_secretsmanager_and_removes_allow_policies() -> None:
    iam_tf = (REPO_ROOT / "infra" / "terraform" / "identity" / "iam_instance.tf").read_text(encoding="utf-8")
    supa_tf = (REPO_ROOT / "infra" / "terraform" / "control-panel" / "supabase.tf").read_text(encoding="utf-8")

    assert "ssm_parameter_policy" not in iam_tf
    assert "spot_supabase_secrets" not in supa_tf
    assert re.search(r'effect\s*=\s*"Deny"', iam_tf, re.IGNORECASE)
    assert "ssm:GetParameter*" in iam_tf
    assert "secretsmanager:*" in iam_tf
