from data.ingest_telemetry import _is_nonfatal_provider_error, _sanitize_provider_error


def test_sanitize_provider_error_redacts_api_key_phrase() -> None:
    raw = "Alpha Vantage info: We detected your API key as NFCJDKAW8G0G5G7L."
    out = _sanitize_provider_error(raw)
    assert out is not None
    assert "NFCJDKAW8G0G5G7L" not in out
    assert "API key as ***" in out


def test_nonfatal_provider_error_detects_rate_limit() -> None:
    msg = "Daily rate limit reached (429). Please upgrade your plan."
    assert _is_nonfatal_provider_error(msg) is True


def test_nonfatal_provider_error_false_for_general_failure() -> None:
    msg = "TLS handshake failed while connecting to provider host"
    assert _is_nonfatal_provider_error(msg) is False
