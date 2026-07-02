from __future__ import annotations

import logging

from data.yfinance_fetch import _ExpectedNoDataFilter


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="yfinance",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_expected_discovery_miss_is_filtered() -> None:
    log_filter = _ExpectedNoDataFilter()

    assert not log_filter.filter(
        _record("$SYRUPUSD: possibly delisted; no price data found (period=5d)")
    )
    assert not log_filter.filter(
        _record("$UNKNOWN: no timezone found, symbol may be delisted")
    )
    assert not log_filter.filter(
        _record(
            'HTTP Error 404: {"error":{"description":'
            '"Quote not found for symbol: UNKNOWN"}}'
        )
    )


def test_unexpected_provider_error_remains_visible() -> None:
    assert _ExpectedNoDataFilter().filter(
        _record("Yahoo request failed with HTTP 500")
    )
