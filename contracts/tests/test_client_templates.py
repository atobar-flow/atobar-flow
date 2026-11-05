import pytest
"""Unit tests for tenant issue, PR, and review audit templates (Issue #321 WP-5 / AC-4)."""

import pathlib

from atobar_contracts.client_templates import (
    render_into,
    render_issue_template,
    render_pr_template,
    render_review_template,
    render_trigger,
)
from atobar_contracts.manifest import TenantManifest


def _manifest() -> TenantManifest:
    return TenantManifest.model_validate(
        {
            "repo": "arsenyspb/ollama-monitor",
            "owner_github_login": "arsenyspb",
            "metrics": [
                {
                    "key": "e2e_install_time_seconds",
                    "run": "echo 1",
                    "comparator": "<=",
                    "threshold": 60,
                }
            ],
        }
    )


def test_ac4_render_into_writes_issue_pr_and_review_audit_templates(tmp_path: pathlib.Path) -> None:
    manifest = _manifest()
    paths = render_into(manifest, "https://cp.example.invalid", tmp_path)

    assert ".github/ISSUE_TEMPLATE/flow-task.md" in paths
    assert ".github/PULL_REQUEST_TEMPLATE.md" in paths
    assert ".github/REVIEW_TEMPLATE.md" in paths

    issue_tpl = render_issue_template(manifest.slug)
    pr_tpl = render_pr_template(manifest.slug)
    review_tpl = render_review_template(manifest.slug)

    assert (tmp_path / ".github/ISSUE_TEMPLATE/flow-task.md").read_text() == issue_tpl
    assert (tmp_path / ".github/PULL_REQUEST_TEMPLATE.md").read_text() == pr_tpl
    assert (tmp_path / ".github/REVIEW_TEMPLATE.md").read_text() == review_tpl

    # Verify simplified 2-section human issue template without hardcoded frontmatter labels
    frontmatter = issue_tpl.split("---")[1]
    assert "labels:" not in frontmatter
    assert "## Goal — What to build or fix" in issue_tpl
    assert "## Acceptance Criteria — What done looks like" in issue_tpl

    trigger_yml = render_trigger(manifest.slug, "https://cp.example.invalid")
    for required_label in ("flow:feature", "flow:fix", "flow:plan", "flow:approved", "flow:resume"):
        assert f"github.event.label.name == '{required_label}'" in trigger_yml
    assert "flow:docs" not in trigger_yml

    assert "Per-Detail Corner-Case Audit" in pr_tpl
    assert "Assumption & Simplification Audit" in pr_tpl

    assert "Corner-Case Verification" in review_tpl
    assert "Assumption & Simplification Audit" in review_tpl


@pytest.mark.parametrize(
    "run_cmd, expected",
    [("echo noise; echo 3.5", "3.5"), ("grep -c proxy monitor.py || true", "0"), ("echo 1 && echo 2", "2")],
)
def test_rendered_metric_step_takes_last_line_of_the_whole_command(tmp_path, run_cmd, expected):
    """Executes the generated CI step body with GitHub's default `bash -eo pipefail`."""
    import subprocess

    from atobar_contracts.client_templates import render_metrics
    from atobar_contracts.manifest import TenantManifest

    m = TenantManifest.model_validate({
        "repo": "o/r", "owner_github_login": "o",
        "metrics": [{"key": "k", "run": run_cmd, "comparator": "<=", "threshold": 1}],
    })
    lines = render_metrics(m, "https://cp").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip().startswith("v=$("))
    end = next(i for i in range(start, len(lines)) if "tail -n1" in lines[i])
    snippet = "\n".join(ln.strip() for ln in lines[start : end + 1])
    (tmp_path / "monitor.py").write_text("print('hi')\n")
    out = subprocess.run(["bash", "-eo", "pipefail", "-c", snippet + '\nprintf %s "$v"'], cwd=tmp_path,
                         capture_output=True, text=True, check=True)
    assert out.stdout == expected


def test_every_label_forwarded_by_trigger_is_in_the_label_catalog() -> None:
    import re

    from atobar_contracts.issue_states import FLOW_LABEL_CATALOG

    forwarded = set(re.findall(r"label\.name == '(flow:[a-z-]+)'", render_trigger("owner-repo", "https://cp.example")))
    assert forwarded, "trigger must forward at least the human work labels"
    assert forwarded <= {entry["name"] for entry in FLOW_LABEL_CATALOG}


@pytest.mark.parametrize(
    "bad_cmd",
    [
        "echo 1\n      - name: injected",
        "echo 1\r",
        "echo ${{ secrets.ATOBAR_TENANT_TOKEN }}",
    ],
)
def test_j42_wp4_metric_and_setup_commands_reject_newlines_and_gha_expressions(bad_cmd: str) -> None:
    from atobar_contracts.manifest import Metric, MetricsRuntime, validate_compiled_metric_command

    assert validate_compiled_metric_command(bad_cmd)
    with pytest.raises(ValueError):
        Metric(key="m1", run=bad_cmd, comparator=">=", threshold=1.0)
    with pytest.raises(ValueError):
        MetricsRuntime(setup=[bad_cmd])
