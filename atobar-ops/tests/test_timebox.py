from atobar_ops import timebox


def test_default_timeout_claudecode(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    assert timebox.default_timeout() == 100


def test_default_timeout_pi_gemini(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    assert timebox.default_timeout() == 240


def test_resolve_timeout_hard_cap(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    assert timebox.resolve_timeout(10000) == timebox.HARD_CAP_SECONDS


def test_resolve_timeout_uses_default_when_unset(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    assert timebox.resolve_timeout(None) == 100


def test_run_never_raises_on_timeout():
    res = timebox.run(["sleep", "5"], timeout=0.05)
    assert res.returncode != 0
    assert "timed out" in res.stderr


def test_run_returns_completed_process_on_success():
    res = timebox.run(["echo", "hi"], timeout=5)
    assert res.returncode == 0
    assert res.stdout.strip() == "hi"


def test_exit_codes_are_distinct():
    assert len({timebox.EXIT_OK, timebox.EXIT_ERROR, timebox.EXIT_TIMEOUT}) == 3


def test_deadline_expired_and_remaining():
    d = timebox.Deadline(1)
    assert not d.expired()
    assert d.remaining() <= 1
    d2 = timebox.Deadline(0)
    assert d2.expired()
