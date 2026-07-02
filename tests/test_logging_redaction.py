from __future__ import annotations

from run import _redact_log_message


def test_redacts_secret_query_parameters() -> None:
    raw = (
        "GET https://example.test/news?category=general&token=secret-one"
        "&apiKey=secret-two&safe=value"
    )
    output = _redact_log_message(raw)

    assert "secret-one" not in output
    assert "secret-two" not in output
    assert "safe=value" in output
    assert output.count("[REDACTED]") == 2


def test_redacts_authorization_headers() -> None:
    output = _redact_log_message("Authorization: Bearer very-secret")

    assert "very-secret" not in output
    assert output == "Authorization=[REDACTED]"
