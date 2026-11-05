import shutil
import subprocess
from pathlib import Path

import pytest

from control_panel.client_templates import render_trigger


def test_j09_ac4_generated_workflow_passes_actionlint():
    if not shutil.which("actionlint"):
        pytest.skip("actionlint not found in PATH")

    trigger_yaml = render_trigger("owner__repo", "https://cp.example.com")

    tmp = Path("/tmp/rendered-trigger.yml")
    tmp.write_text(trigger_yaml)

    # We test it with actionlint
    cmd = ["actionlint"]
    if shutil.which("shellcheck"):
        cmd.append("-shellcheck=shellcheck")
    cmd.append(str(tmp))

    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"actionlint failed:\n{res.stdout}\n{res.stderr}"

