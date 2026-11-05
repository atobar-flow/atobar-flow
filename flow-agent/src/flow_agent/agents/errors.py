"""Sanitized error classification for provider failures (429, 401, timeouts, quotas)."""

import re
from typing import Any, Optional, Tuple

from flow_agent.agents.base import HaltReason


def classify_provider_error(exc: BaseException) -> Tuple[Optional[HaltReason], Optional[str]]:
    """Maps exceptions to (HaltReason, sanitized_detail) or (None, None).

    Sanitized detail is strictly single-line and excludes raw json/braces.
    """
    if exc is None:
        return None, None

    # Check status code or attributes
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    err_str = str(exc)

    # Inspect dictionary response if present
    resp_data: dict[str, Any] = {}
    response_json = getattr(exc, "response_json", None)
    if isinstance(response_json, dict):
        resp_data = response_json
    elif hasattr(exc, "response") and hasattr(getattr(exc, "response"), "json"):
        try:
            resp_data = getattr(exc, "response").json()
        except Exception:
            pass

    # Look for status code in string if not directly found
    err_lower = err_str.lower()
    if status_code is None:
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "ratelimiterror" in err_lower:
            status_code = 429
        elif (
            "401" in err_str
            or "UNAUTHENTICATED" in err_str
            or "Unauthorized" in err_str
            or "authenticationerror" in err_lower
            or "invalid_api_key" in err_lower
        ):
            status_code = 401
        elif (
            "400" in err_str
            or "API_KEY_INVALID" in err_str
            or "api key not valid" in err_lower
            or "INVALID_ARGUMENT" in err_str
            or "badrequesterror" in err_lower
        ):
            status_code = 400
        elif "403" in err_str or "PERMISSION_DENIED" in err_str:
            status_code = 403
        elif "404" in err_str or "NOT_FOUND" in err_str or "notfounderror" in err_lower or "is not found" in err_lower:
            status_code = 404
        elif "503" in err_str or "UNAVAILABLE" in err_str or "502" in err_str or "504" in err_str:
            status_code = 503

    if status_code is None and not any(
        kw in err_lower
        for kw in ["quota", "rate limit", "deadline", "timeout", "error calling llm", "litellm.", "provider error"]
    ):
        return None, None

    # Determine reason
    is_quota = status_code == 429 or "quota" in err_str.lower() or "resource_exhausted" in err_str.lower()
    reason: HaltReason = "provider_quota" if is_quota else "provider_error"

    # Extract retry delay and quota metric if available
    retry_delay = None
    quota_metric = None
    model_name = None

    # Try from parsed dictionary details
    details = []
    if isinstance(resp_data.get("error"), dict):
        details = resp_data["error"].get("details", [])
    elif isinstance(resp_data.get("details"), list):
        details = resp_data["details"]

    for d in details:
        if isinstance(d, dict):
            if "retryDelay" in d:
                retry_delay = str(d["retryDelay"])
            if "violations" in d and isinstance(d["violations"], list):
                for v in d["violations"]:
                    if isinstance(v, dict) and "quotaMetric" in v:
                        quota_metric = str(v["quotaMetric"])
                    if isinstance(v, dict) and "quotaDimensions" in v and "model" in v["quotaDimensions"]:
                        model_name = str(v["quotaDimensions"]["model"])

    # Fallback to regex on err_str
    if not retry_delay:
        m = re.search(r"retry(?:Delay| in)?\s*[:=]?\s*([0-9.]+[smh]?)", err_str, re.IGNORECASE)
        if m:
            retry_delay = m.group(1)

    if not quota_metric:
        m = re.search(r"metric:\s*([a-zA-Z0-9_./-]+)", err_str)
        if m:
            quota_metric = m.group(1)

    if not model_name:
        m = re.search(r"model:\s*([a-zA-Z0-9_.-]+)", err_str)
        if m:
            model_name = m.group(1)

    parts = []
    if model_name:
        parts.append(f"model={model_name}")
    if quota_metric:
        parts.append(f"quota={quota_metric}")
    if retry_delay:
        parts.append(f"retry_delay={retry_delay}")

    detail_summary = " ".join(parts) if parts else (f"status={status_code}" if status_code else "provider failure")
    sanitized = f"{reason}: {detail_summary}"

    # Clean any newline or braces
    sanitized = sanitized.replace("\n", " ").replace("{", "").replace("}", "").strip()
    return reason, sanitized
