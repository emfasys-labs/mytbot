"""
core/market_session.py
======================
Market-session validity — *reality*, not a risk cap.

A venue physically cannot transact a closed market. In paper mode the
simulator was filling equity/forex orders on weekends and overnight against
stale last prices, manufacturing fake churn and polluting every P&L number
(realised attribution, win/loss counts, model evidence). A real broker
could not have filled those either, so blocking them is *correctness*, not
a hardcoded limitation — fully consistent with the project's
"no static caps, only market-driven gates" philosophy.

Dependency-free by design: no exchange-calendar package is installed, so
this uses only the stdlib (``zoneinfo``). Conservative / fail-open: it
returns ``False`` only when we are confident the market is closed; unknown
asset classes and 24/7 venues are never blocked.

Toggle: ``MARKET_SESSION_GATE=0`` disables the gate entirely (default on).
``MARKET_SESSION_EXTENDED=1`` widens US equity hours to 04:00–20:00 ET
(pre/post-market) instead of the 09:30–16:00 regular session.
"""

from __future__ import annotations

import os
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# US equity/ETF/option FULL-DAY closures (NYSE/Nasdaq). A factual calendar,
# not a tunable knob. Half-days (early closes) are intentionally treated as
# open — the goal is to stop weekend/overnight stale fills, not microscopic
# half-day precision. Extend this set forward each year.
_US_EQUITY_HOLIDAYS = {
    # 2026
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # Martin Luther King Jr. Day
    "2026-02-16",  # Washington's Birthday
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day (observed Fri Jul 3)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving
    "2026-12-25",  # Christmas
    # 2027 (forward cover so a year roll doesn't silently un-gate holidays)
    "2027-01-01",
    "2027-01-18",
    "2027-02-15",
    "2027-03-26",
    "2027-05-31",
    "2027-06-18",
    "2027-07-05",
    "2027-09-06",
    "2027-11-25",
    "2027-12-24",
}

_CRYPTO_AC = {"crypto", "cryptocurrency", "coin", "digital", "spot_crypto"}
_FX_AC = {"forex", "fx", "currency", "cash"}
_EQUITY_AC = {"equity", "stock", "stocks", "etf", "option", "options", "fund", "index"}


def _gate_enabled() -> bool:
    return os.getenv("MARKET_SESSION_GATE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _extended_hours() -> bool:
    return os.getenv("MARKET_SESSION_EXTENDED", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _norm_ac(asset_class: object) -> str:
    ac = getattr(asset_class, "value", asset_class)
    return str(ac or "").strip().lower()


def is_market_open(
    asset_class: object,
    symbol: str = "",
    now: datetime | None = None,
) -> bool:
    """True if the instrument's venue can actually transact at ``now``.

    Conservative: returns ``False`` only when confident the market is
    closed (US equity/ETF/option outside session or on a holiday, or FX
    over the weekend). 24/7 venues and unclassifiable instruments are
    never blocked (fail-open) so the gate cannot strangle legitimate
    crypto / unknown flow.
    """
    if not _gate_enabled():
        return True
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ac = _norm_ac(asset_class)

    # 24/7 markets — always open.
    if ac in _CRYPTO_AC:
        return True

    # FX / forex: ~24×5. Opens Sun 21:00 UTC, closes Fri 21:00 UTC. The
    # one-hour DST drift is irrelevant to the goal (kill weekend fills).
    if ac in _FX_AC:
        wd = now.weekday()  # Mon=0 … Sun=6
        if wd == 5:  # Saturday — closed
            return False
        if wd == 6:  # Sunday — opens 21:00 UTC
            return now.hour >= 21
        if wd == 4 and now.hour >= 21:  # Friday after 21:00 UTC — closed
            return False
        return True

    # Equity / ETF / option / fund → US session in America/New_York,
    # Mon–Fri, excluding full-day holidays.
    if ac in _EQUITY_AC:
        et = now.astimezone(_ET)
        if et.weekday() >= 5:  # Sat/Sun
            return False
        if et.strftime("%Y-%m-%d") in _US_EQUITY_HOLIDAYS:
            return False
        if _extended_hours():
            open_t, close_t = time(4, 0), time(20, 0)
        else:
            open_t, close_t = time(9, 30), time(16, 0)
        return open_t <= et.time() <= close_t

    # Unknown asset class → fail-open (do not block what we can't classify).
    return True


def market_closed_reason(
    asset_class: object,
    symbol: str = "",
    now: datetime | None = None,
) -> str | None:
    """Human-readable reason string when closed, else ``None``."""
    if is_market_open(asset_class, symbol, now):
        return None
    ac = _norm_ac(asset_class)
    when = (now or datetime.now(timezone.utc)).astimezone(_ET)
    return (
        f"market_closed:{ac or 'unknown'}:"
        f"{when.strftime('%a %Y-%m-%d %H:%M')}_ET"
    )
