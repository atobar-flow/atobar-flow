import hashlib
import secrets
import threading
import time
from collections.abc import Hashable
from datetime import datetime, timedelta, timezone


def generate_run_credentials(ttl_minutes: int = 120) -> tuple[str, str, str, str]:
    """Generates a run ID, a plain credential, its hash, and an expiration timestamp."""
    run_id = f"run_{secrets.token_hex(8)}"
    run_cred = f"atb_run_{secrets.token_hex(16)}"
    cred_hash = hashlib.sha256(run_cred.encode("utf-8")).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=max(1, ttl_minutes))).isoformat()
    return run_id, run_cred, cred_hash, expires_at


GITHUB_TOKEN_MINT_INTERVAL_S = 60.0

ROLE_GITHUB_PERMISSIONS: dict[str, dict[str, str]] = {
    "planner": {
        "contents": "read",
        "pull_requests": "read",
        "issues": "write",
        "checks": "read",
        "metadata": "read",
    },
    "metric_compiler": {
        "contents": "read",
        "pull_requests": "read",
        "issues": "write",
        "checks": "read",
        "metadata": "read",
    },
    "developer": {
        "contents": "write",
        "pull_requests": "write",
        "issues": "write",
        "checks": "read",
        "metadata": "read",
    },
    "evaluator": {
        "contents": "read",
        "pull_requests": "write",
        "issues": "write",
        "checks": "write",
        "metadata": "read",
    },
    "arbitrator": {
        "contents": "write",
        "pull_requests": "write",
        "issues": "write",
        "checks": "write",
        "metadata": "read",
    },
    "onboarder": {
        "contents": "write",
        "workflows": "write",
        "pull_requests": "write",
        "issues": "write",
        "checks": "read",
        "metadata": "read",
    },
}


class MintRateLimiter:
    """Thread-safe, self-pruning "at most one mint per interval per key" limiter.

    Process-local: correct only while the service runs as a single uvicorn worker / single replica
    (see docs/CROSS_SERVICE_CHANGES.md §4). It resets on restart; multi-worker needs an atomic DB claim.
    """

    def __init__(self, interval_s: float = GITHUB_TOKEN_MINT_INTERVAL_S) -> None:
        self.interval_s = interval_s
        self._last: dict[Hashable, float] = {}
        self._lock = threading.Lock()

    def try_acquire(self, key: Hashable, now: float | None = None) -> bool:
        """Atomically records a mint for `key` and returns True, or returns False if one happened within the interval."""
        now = time.monotonic() if now is None else now
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self.interval_s:
                return False
            self._last = {k: t for k, t in self._last.items() if now - t < self.interval_s}
            self._last[key] = now
            return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._last)
