from flow_agent.agents.errors import classify_provider_error


def test_classify_provider_error_429():
    class FakeHttpError(Exception):
        status_code = 429
        response_json = {
            "error": {
                "code": 429,
                "message": "Resource exhausted",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {
                                "quotaMetric": (
                                    "generativelanguage.googleapis.com/generate_content_paid_tier_3_input_token_count"
                                )
                            }
                        ],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "24s"},
                ],
            }
        }

    reason, detail = classify_provider_error(FakeHttpError("429 Resource Exhausted"))
    assert reason == "provider_quota"
    assert "\n" not in detail
    assert "{" not in detail
    assert "24s" in detail or "24" in detail


def test_classify_provider_error_401():
    class FakeAuthError(Exception):
        status_code = 401

    reason, detail = classify_provider_error(FakeAuthError("Unauthorized"))
    assert reason == "provider_error"
    assert "\n" not in detail
    assert "{" not in detail


def test_classify_provider_error_non_provider():
    reason, detail = classify_provider_error(ValueError("Invalid argument"))
    assert reason is None
    assert detail is None
