import atexit
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


def _truncate_val(val: Any) -> Any:
    if isinstance(val, str) and len(val) > 4000:
        return val[:4000]
    return val


class EventSink:
    """Agent-side append-only telemetry event sink for the run_events stream."""

    def __init__(
        self,
        cp_url: Optional[str] = None,
        run_id: Optional[str] = None,
        run_cred: Optional[str] = None,
    ) -> None:
        self.cp_url = cp_url or os.environ.get("CONTROL_PANEL_URL") or os.environ.get("CP_URL")
        self.run_id = run_id or os.environ.get("FLOW_RUN_ID")
        self.run_cred = run_cred or os.environ.get("FLOW_RUN_CREDENTIAL")
        self.enabled = bool(self.cp_url and self.run_id and self.run_cred)

        self._lock = threading.Lock()
        self._buffer: list[dict[str, Any]] = []
        self._stop_event = threading.Event()
        self._flusher_thread: Optional[threading.Thread] = None

        if self.enabled and "pytest" not in sys.modules:
            self._flusher_thread = threading.Thread(target=self._flusher_loop, daemon=True)
            self._flusher_thread.start()
            atexit.register(self.flush)

    def emit(self, event_type: str, **payload: Any) -> None:
        if not self.enabled:
            return

        cleaned_payload = {k: _truncate_val(v) for k, v in payload.items()}
        event = {
            "seq": time.time_ns(),
            "kind": event_type,
            "ts": datetime.now(timezone.utc).isoformat(),
            "payload": cleaned_payload,
        }

        should_flush = False
        with self._lock:
            self._buffer.append(event)
            if len(self._buffer) >= 50:
                should_flush = True

        if should_flush:
            self.flush()

    def flush(self) -> None:
        if not self.enabled:
            return

        batch: list[dict[str, Any]] = []
        with self._lock:
            if not self._buffer:
                return
            batch = self._buffer[:]
            self._buffer.clear()

        try:
            url = f"{self.cp_url.rstrip('/') if self.cp_url else ''}/v1/runs/{self.run_id}/events"
            headers = {"Authorization": f"Bearer {self.run_cred}"}
            inst_id = os.environ.get("INSTANCE_ID", "").strip()
            if inst_id:
                headers["X-Instance-ID"] = inst_id
            resp = httpx.post(url, json={"events": batch}, headers=headers, timeout=5.0)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning(
                "EventSink flush failed for run_id=%s (%d events dropped): %s",
                self.run_id,
                len(batch),
                str(exc)[:500],
            )

    def _flusher_loop(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(5.0)
            try:
                self.flush()
            except Exception:
                pass

    def close(self) -> None:
        self._stop_event.set()
        self.flush()


_GLOBAL_SINK: Optional[EventSink] = None


def init(
    cp_url: Optional[str] = None,
    run_id: Optional[str] = None,
    run_cred: Optional[str] = None,
) -> EventSink:
    global _GLOBAL_SINK
    _GLOBAL_SINK = EventSink(cp_url=cp_url, run_id=run_id, run_cred=run_cred)
    return _GLOBAL_SINK


def get_sink() -> EventSink:
    global _GLOBAL_SINK
    if _GLOBAL_SINK is None:
        _GLOBAL_SINK = EventSink()
    return _GLOBAL_SINK


def emit(event_type: str, **payload: Any) -> None:
    sink = get_sink()
    sink.emit(event_type, **payload)


def flush() -> None:
    sink = get_sink()
    sink.flush()
