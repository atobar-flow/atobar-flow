import time
from typing import Optional

from flow_agent.core.config import get_flow_config


class CircuitBreakerException(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Circuit Breaker Tripped: {reason}")


class CircuitBreaker:
    def __init__(self, config_path: str = "", max_wall_clock_minutes: Optional[int] = None):
        config = get_flow_config(config_path)
        self.max_ci_retries = int(config.get("max_ci_retries", 3))
        if max_wall_clock_minutes is not None:
            self.max_wall_clock_minutes = max_wall_clock_minutes
        else:
            self.max_wall_clock_minutes = int(config.get("max_wall_clock_minutes", 60))
        self.ci_failures = 0
        self.start_time = time.time()

    def check_time(self):
        """Checks if the wall clock time has exceeded the configured maximum."""
        elapsed_minutes = (time.time() - self.start_time) / 60.0
        if elapsed_minutes > self.max_wall_clock_minutes:
            raise CircuitBreakerException("WALL_CLOCK_TIMEOUT")

    def record_ci_failure(self):
        """Increments the CI failure count and checks if the threshold is breached."""
        self.ci_failures += 1
        if self.ci_failures >= self.max_ci_retries:
            raise CircuitBreakerException("MAX_CI_FAILURES")
