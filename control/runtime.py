"""
Lightweight runtime registry for shared service instances.
"""

from __future__ import annotations

from typing import Any

_RISK_ENGINE: Any = None
_EXECUTION_ENGINE: Any = None
_BROKER_MANAGER: Any = None


def set_risk_engine(engine: Any) -> None:
    global _RISK_ENGINE
    _RISK_ENGINE = engine


def get_risk_engine() -> Any:
    return _RISK_ENGINE


def set_execution_engine(engine: Any) -> None:
    global _EXECUTION_ENGINE
    _EXECUTION_ENGINE = engine


def get_execution_engine() -> Any:
    return _EXECUTION_ENGINE


def set_broker_manager(bm: Any) -> None:
    global _BROKER_MANAGER
    _BROKER_MANAGER = bm


def get_broker_manager() -> Any:
    return _BROKER_MANAGER


def current_active_brokers() -> set[str] | None:
    """Lowercased broker names currently in dashboard/accounting scope.

    ``None`` means the broker manager is not registered or coverage is full
    (no filtering needed). A possibly-empty ``set`` means partial coverage:
    only these names should be summed into NAV-denominated portfolio totals.
    Mirrors :func:`api.server._current_nav_broker_filter` so the trading
    loop / risk engine and the dashboard / accounting paths agree on which
    brokers are "in scope" for a given tick.
    """
    bm = _BROKER_MANAGER
    if bm is None:
        return None
    report = getattr(bm, "report", None)
    if report is None:
        return None
    try:
        cov = report.coverage()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(cov, dict) or bool(cov.get("full")):
        return None
    included = cov.get("included") or []
    return {str(n).strip().lower() for n in included if str(n).strip()}


def coverage_is_full() -> bool:
    """``True`` when every configured broker is connected + balance_ready.

    Used by the daily-PnL writer to decide whether a row reflects the full
    book or a partial coverage window (in which case ``portfolio_value`` is
    not written so the HWM does not regress).
    """
    bm = _BROKER_MANAGER
    if bm is None:
        return True
    report = getattr(bm, "report", None)
    if report is None:
        return True
    try:
        cov = report.coverage()
    except Exception:  # noqa: BLE001
        return True
    return bool(isinstance(cov, dict) and cov.get("full", True))

