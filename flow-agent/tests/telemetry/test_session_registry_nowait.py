"""Background heartbeats must never block the agent loop, and must not reorder terminal status."""

import asyncio
import os
import threading
from typing import Any
from unittest.mock import patch

import pytest

from flow_agent.agents.base import RoleTask, RuntimeHooks
from flow_agent.agents.nanobot_runtime import FlowHook
from flow_agent.telemetry import session_registry as sr_mod
from flow_agent.telemetry.session_registry import SessionRegistry

_ENV = {"CONTROL_PANEL_URL": "http://cp", "FLOW_RUN_ID": "run-1", "FLOW_RUN_CREDENTIAL": "cred"}


class _GatedSender:
    """Stands in for the blocking HTTP POST; each send waits until the test releases it."""

    def __init__(self, registry: SessionRegistry) -> None:
        self.registry = registry
        self.sent: list[dict[str, Any]] = []
        self.gate = threading.Event()
        self.started = threading.Event()

    def __call__(self) -> None:
        payload = self.registry._payload()
        self.started.set()
        self.gate.wait(timeout=10)
        self.sent.append(payload)


def _registry(monkeypatch: pytest.MonkeyPatch) -> tuple[SessionRegistry, _GatedSender]:
    with patch.dict(os.environ, _ENV, clear=True):
        reg = SessionRegistry("i-1", "o/r", 7)
    sender = _GatedSender(reg)
    monkeypatch.setattr(reg, "_upsert_sync", sender)
    return reg, sender


def _hook(reg: SessionRegistry, tmp_path: Any) -> FlowHook:
    task = RoleTask(
        role="developer", system_prompt="", context="", task_prompt="",
        workspace_dir=str(tmp_path), allowed_tools=frozenset(["todo_write"]),
        model_name="gemini-2.5-flash", done_envelope="flow-ack",
    )
    return FlowHook(task=task, hooks=RuntimeHooks(registry=reg))


@pytest.mark.asyncio
async def test_before_iteration_returns_while_heartbeat_is_still_in_flight(monkeypatch, tmp_path):
    reg, sender = _registry(monkeypatch)
    hook = _hook(reg, tmp_path)

    await hook.before_iteration(None)  # would hang here if the send were awaited
    await asyncio.to_thread(sender.started.wait, 10)
    assert sender.sent == []  # still blocked in the "network"

    sender.gate.set()
    await reg.drain()
    assert len(sender.sent) == 1
    assert sender.sent[0]["iterations"] == 1


@pytest.mark.asyncio
async def test_unforced_heartbeats_are_throttled(monkeypatch):
    reg, sender = _registry(monkeypatch)
    sender.gate.set()
    clock = {"t": 1000.0}
    monkeypatch.setattr(sr_mod.time, "monotonic", lambda: clock["t"])

    reg.heartbeat_nowait(min_interval_s=15)
    await reg.drain()
    clock["t"] += 5
    reg.heartbeat_nowait(min_interval_s=15)  # inside the window: dropped
    await reg.drain()
    assert len(sender.sent) == 1

    clock["t"] += 11
    reg.heartbeat_nowait(min_interval_s=15)
    await reg.drain()
    assert len(sender.sent) == 2


@pytest.mark.asyncio
async def test_forced_heartbeat_during_send_is_coalesced_into_one_follow_up(monkeypatch):
    reg, sender = _registry(monkeypatch)

    reg.heartbeat_nowait()
    await asyncio.to_thread(sender.started.wait, 10)
    reg.current_todo = "write failing test"
    reg.heartbeat_nowait(force=True)
    reg.current_todo = "implement flag"
    reg.heartbeat_nowait(force=True)
    sender.gate.set()
    await reg.drain()

    assert len(sender.sent) == 2  # original + exactly one coalesced resend
    assert sender.sent[-1]["current_todo"] == "implement flag"


@pytest.mark.asyncio
async def test_terminate_waits_for_in_flight_running_heartbeat(monkeypatch):
    reg, sender = _registry(monkeypatch)

    reg.heartbeat_nowait()
    await asyncio.to_thread(sender.started.wait, 10)
    terminate = asyncio.create_task(reg.terminate("Halted", reason="stall"))
    await asyncio.sleep(0)
    sender.gate.set()
    await terminate

    assert [p["status"] for p in sender.sent] == ["Running", "Halted"]


@pytest.mark.asyncio
async def test_heartbeat_nowait_is_noop_when_disabled():
    with patch.dict(os.environ, {}, clear=True):
        reg = SessionRegistry("i-1", "o/r", 7)
    reg.heartbeat_nowait(force=True)
    assert reg._bg_task is None


@pytest.mark.asyncio
async def test_awaited_heartbeat_lands_after_in_flight_background_send(monkeypatch):
    reg, sender = _registry(monkeypatch)

    reg.heartbeat_nowait()
    await asyncio.to_thread(sender.started.wait, 10)
    final = asyncio.create_task(reg.heartbeat(usage={"total": {"total_tokens": 99}}))
    await asyncio.sleep(0)
    sender.gate.set()
    await final

    assert [p["usage"] for p in sender.sent] == [None, {"total": {"total_tokens": 99}}]
