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

    ``None`` means "don't filter" — either the broker manager isn't
    registered yet, no brokers have been configured at all (very early
    startup), or coverage is full. A non-None set means partial coverage:
    only these names should be summed into NAV-denominated portfolio
    totals. Mirrors :func:`api.server._current_nav_broker_filter` so the
    trading loop / risk engine and the dashboard / accounting paths agree
    on which brokers are "in scope" for a given tick.

    Critically: an empty ``configured`` list is NOT treated as partial
    coverage. ``BrokerManager.__init__`` registers the broker manager
    before ``discover_and_connect`` runs, so during a brief startup
    window ``report.brokers`` is empty — returning ``set()`` then would
    classify every persisted position as offline and zero out
    ``current_gross_exposure``, which in turn makes the trading loop
    behave as if the book were empty.
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
    if not isinstance(cov, dict):
        return None
    if not cov.get("configured"):
        # Report not populated yet — fall back to "no filter".
        return None
    if bool(cov.get("full")):
        return None
    included = cov.get("included") or []
    return {str(n).strip().lower() for n in included if str(n).strip()}


def coverage_is_full() -> bool:
    """``True`` when every configured broker is connected + balance_ready,
    OR when no brokers are configured yet (startup window).

    Used by the daily-PnL writer to stamp a ``partial_coverage`` flag.
    The actual HWM-ratchet guard lives in the *reader* (the HWM query
    filters out rows stamped partial), not in the writer — so the
    heartbeat can still persist the active-scope NAV during a gap and
    operators see a live row rather than a zero.
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
    if not isinstance(cov, dict):
        return True
    if not cov.get("configured"):
        return True
    return bool(cov.get("full", True))

