import logging

import httpx

from flow_agent.telemetry.session_registry import SessionRegistry


def test_heartbeat_error_400_logs_warning_and_does_not_crash(monkeypatch, caplog):
    monkeypatch.setenv("CONTROL_PANEL_URL", "https://cp.example.com")
    monkeypatch.setenv("FLOW_RUN_ID", "run-test-400")
    monkeypatch.setenv("FLOW_RUN_CREDENTIAL", "cred-test")

    def mock_post(*args, **kwargs):
        request = httpx.Request("POST", args[0])
        response = httpx.Response(400, text="Bad Request: column unknown", request=request)
        return response

    monkeypatch.setattr(httpx, "post", mock_post)

    registry = SessionRegistry("inst-1", "owner/repo", 1)
    with caplog.at_level(logging.WARNING):
        registry._upsert_sync()

    assert "run-test-400" in caplog.text
    assert "400" in caplog.text
    assert "Bad Request" in caplog.text


def test_heartbeat_error_500_logs_warning_and_does_not_crash(monkeypatch, caplog):
    monkeypatch.setenv("CONTROL_PANEL_URL", "https://cp.example.com")
    monkeypatch.setenv("FLOW_RUN_ID", "run-test-500")
    monkeypatch.setenv("FLOW_RUN_CREDENTIAL", "cred-test")

    def mock_post(*args, **kwargs):
        request = httpx.Request("POST", args[0])
        response = httpx.Response(500, text="Internal Server Error: database connection lost", request=request)
        return response

    monkeypatch.setattr(httpx, "post", mock_post)

    registry = SessionRegistry("inst-2", "owner/repo", 2)
    with caplog.at_level(logging.WARNING):
        registry._upsert_sync()

    assert "run-test-500" in caplog.text
    assert "500" in caplog.text
    assert "Internal Server Error" in caplog.text


def test_heartbeat_network_exception_logs_warning_and_does_not_crash(monkeypatch, caplog):
    monkeypatch.setenv("CONTROL_PANEL_URL", "https://cp.example.com")
    monkeypatch.setenv("FLOW_RUN_ID", "run-test-exc")
    monkeypatch.setenv("FLOW_RUN_CREDENTIAL", "cred-test")

    def mock_post(*args, **kwargs):
        raise httpx.ConnectTimeout("Connection timed out")

    monkeypatch.setattr(httpx, "post", mock_post)

    registry = SessionRegistry("inst-3", "owner/repo", 3)
    with caplog.at_level(logging.WARNING):
        registry._upsert_sync()

    assert "run-test-exc" in caplog.text
    assert "timed out" in caplog.text.lower()
