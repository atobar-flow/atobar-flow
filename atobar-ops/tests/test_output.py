import argparse
import json

from atobar_ops import output


def _args(**kw):
    ns = argparse.Namespace(json=False, verbose=False, max_chars=None)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_redacts_planted_secret_in_text_output(capsys):
    planted = "sk-ant-api03-THIS-IS-A-PLANTED-TEST-SECRET-VALUE"
    output.emit(_args(), [f"token={planted}"])
    out = capsys.readouterr().out
    assert planted not in out
    assert "[REDACTED]" in out


def test_redacts_planted_secret_in_json_output(capsys):
    planted = "ghp_1234567890abcdefPLANTEDSECRET"
    output.emit(_args(json=True), [], data={"key": planted})
    out = capsys.readouterr().out
    assert planted not in out
    assert "[REDACTED]" in out
    json.loads(out)  # still valid JSON after redaction


def test_default_budget_truncates():
    lines = ["x" * 10000]
    text = output.render_text(lines, output.DEFAULT_MAX_CHARS)
    assert len(text) <= output.DEFAULT_MAX_CHARS + 120
    assert "truncated" in text


def test_verbose_lifts_budget():
    args = _args(verbose=True)
    assert output.budget(args) == output.VERBOSE_MAX_CHARS


def test_max_chars_override():
    args = _args(max_chars=50)
    assert output.budget(args) == 50
