import logging
from unittest.mock import patch

import httpx

from flow_agent.telemetry.event_sink import EventSink


def test_event_sink_disabled_without_envs():
    sink = EventSink()
    # Without envs set, enabled is False
    assert not sink.enabled

    # emit and flush do no HTTP
    with patch("httpx.post") as mock_post:
        sink.emit("test.event", foo="bar")
        sink.flush()
        mock_post.assert_not_called()


def test_event_sink_batches_and_flushes(monkeypatch):
    monkeypatch.setenv("CONTROL_PANEL_URL", "https://cp.example.com")
    monkeypatch.setenv("FLOW_RUN_ID", "run-sink-123")
    monkeypatch.setenv("FLOW_RUN_CREDENTIAL", "cred-sink-456")

    sink = EventSink()
    assert sink.enabled

    posted_batches = []

    def mock_post(url, json=None, headers=None, timeout=None):
        posted_batches.append((url, json, headers))
        req = httpx.Request("POST", url)
        return httpx.Response(200, json={"status": "ok"}, request=req)

    with patch("httpx.post", side_effect=mock_post):
        sink.emit("lifecycle.start", role="developer")
        sink.emit("model_call", tokens=50)
        sink.flush()

    assert len(posted_batches) == 1
    url, data, headers = posted_batches[0]
    assert url == "https://cp.example.com/v1/runs/run-sink-123/events"
    assert headers["Authorization"] == "Bearer cred-sink-456"
    assert len(data["events"]) == 2
    assert data["events"][0]["kind"] == "lifecycle.start"
    assert data["events"][1]["kind"] == "model_call"
    assert data["events"][0]["seq"] > 0


def test_event_sink_post_failure_logged_and_dropped_not_raised(monkeypatch, caplog):
    monkeypatch.setenv("CONTROL_PANEL_URL", "https://cp.example.com")
    monkeypatch.setenv("FLOW_RUN_ID", "run-sink-fail")
    monkeypatch.setenv("FLOW_RUN_CREDENTIAL", "cred-fail")

    sink = EventSink()

    def mock_post_fail(*args, **kwargs):
        raise httpx.ConnectTimeout("Connection timed out")

    with patch("httpx.post", side_effect=mock_post_fail):
        sink.emit("test.event", fail=True)
        with caplog.at_level(logging.WARNING):
            # flush should catch exception, log warning, drop batch and not raise
            sink.flush()

    assert "run-sink-fail" in caplog.text


def test_event_sink_truncates_heads_over_4000_chars(monkeypatch):
    monkeypatch.setenv("CONTROL_PANEL_URL", "https://cp.example.com")
    monkeypatch.setenv("FLOW_RUN_ID", "run-sink-trunc")
    monkeypatch.setenv("FLOW_RUN_CREDENTIAL", "cred-trunc")

    sink = EventSink()
    posted_batches = []

    def mock_post(url, json=None, headers=None, timeout=None):
        posted_batches.append(json)
        req = httpx.Request("POST", url)
        return httpx.Response(200, json={"status": "ok"}, request=req)

    long_str = "x" * 6000
    with patch("httpx.post", side_effect=mock_post):
        sink.emit("tool_call", name="test_tool", args_head=long_str, result_head=long_str)
        sink.flush()

    assert len(posted_batches) == 1
    event = posted_batches[0]["events"][0]
    assert len(event["payload"]["args_head"]) <= 4000
    assert len(event["payload"]["result_head"]) <= 4000
