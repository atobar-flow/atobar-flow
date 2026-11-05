import time

import pytest

from flow_agent.core.watchdog import CircuitBreaker, CircuitBreakerException


def test_circuit_breaker_time_safe():
    breaker = CircuitBreaker("non_existent_config.json")
    breaker.start_time = time.time() - (30 * 60) # 30 minutes ago
    # Should not raise
    breaker.check_time()

def test_circuit_breaker_time_breached():
    breaker = CircuitBreaker("non_existent_config.json")
    breaker.start_time = time.time() - (61 * 60) # 61 minutes ago
    with pytest.raises(CircuitBreakerException) as excinfo:
        breaker.check_time()
    assert excinfo.value.reason == "WALL_CLOCK_TIMEOUT"

def test_circuit_breaker_ci_retries_safe():
    breaker = CircuitBreaker("non_existent_config.json")
    breaker.record_ci_failure()
    breaker.record_ci_failure()
    assert breaker.ci_failures == 2

def test_circuit_breaker_ci_retries_breached():
    breaker = CircuitBreaker("non_existent_config.json")
    breaker.record_ci_failure()
    breaker.record_ci_failure()
    with pytest.raises(CircuitBreakerException) as excinfo:
        breaker.record_ci_failure()
    assert excinfo.value.reason == "MAX_CI_FAILURES"

def test_circuit_breaker_loads_config(tmp_path):
    config_file = tmp_path / "flow.config.json"
    config_file.write_text('{"max_ci_retries": 1, "max_wall_clock_minutes": 5}')

    breaker = CircuitBreaker(str(config_file))
    assert breaker.max_ci_retries == 1
    assert breaker.max_wall_clock_minutes == 5

    with pytest.raises(CircuitBreakerException):
        breaker.record_ci_failure()
