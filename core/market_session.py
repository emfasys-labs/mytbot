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
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# ── Per-venue session policy (config/market_hours.yaml) ──────────────────
# Purely additive: governs only the NEW broker-aware ``is_tradeable``.
# ``is_market_open`` (the proven, deployed asset-class gate used by the
# execution safety-net) is intentionally left byte-identical. Built-in
# defaults below make crypto-only venues 24/7 even with no config file.
_DEFAULT_BROKER_SESSION: dict[str, str] = {
    "kraken": "always",
    "binance": "always",
    "bybit": "always",
    "ibkr": "by_asset_class",
    "alpaca": "by_asset_class",
}


@lru_cache(maxsize=1)
def _broker_session_map() -> dict[str, str]:
    """Load broker→session-policy from config/market_hours.yaml, falling
    back to the built-in defaults. Cached; never raises."""
    out = dict(_DEFAULT_BROKER_SESSION)
    try:
        import yaml  # local import keeps core dependency-light

        p = Path(
            os.getenv("MARKET_HOURS_CONFIG", "config/market_hours.yaml")
        )
        if p.is_file():
            cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            for b, spec in (cfg.get("brokers") or {}).items():
                if isinstance(spec, dict) and spec.get("session"):
                    out[str(b).strip().lower()] = str(spec["session"]).strip().lower()
            dflt = cfg.get("default")
            if isinstance(dflt, dict) and dflt.get("session"):
                out["__default__"] = str(dflt["session"]).strip().lower()
    except Exception:  # noqa: BLE001 — config is best-effort; defaults stand
        pass
    return out

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


def is_tradeable(
    broker: object,
    asset_class: object,
    symbol: str = "",
    now: datetime | None = None,
) -> bool:
    """Broker-aware tradeability — the single authority for "can this
    venue transact this instrument *now*".

    Resolves the broker's session policy from config/market_hours.yaml
    (built-in defaults otherwise):
      * ``always``         → 24/7 venue (crypto exchanges) → True.
      * ``by_asset_class`` → defer to the proven ``is_market_open``
        (US equity RTH+holidays / FX 24x5 / crypto 24/7).

    Used upstream (candidate/opportunity selection, the harvest/stop/
    de-risk monitors) AND at the execution gate, so the whole pipeline
    decides on the same session truth instead of selecting instruments
    it can't act on and discovering it only at the final reject.
    Fail-open: gate disabled or unclassifiable → True.
    """
    if not _gate_enabled():
        return True
    b = str(getattr(broker, "value", broker) or "").strip().lower()
    smap = _broker_session_map()
    policy = smap.get(b) or smap.get("__default__", "by_asset_class")
    if policy == "always":
        return True
    return is_market_open(asset_class, symbol, now)


def not_tradeable_reason(
    broker: object,
    asset_class: object,
    symbol: str = "",
    now: datetime | None = None,
) -> str | None:
    """Reason string when an instrument is NOT tradeable now, else None."""
    if is_tradeable(broker, asset_class, symbol, now):
        return None
    b = str(getattr(broker, "value", broker) or "").strip().lower()
    base = market_closed_reason(asset_class, symbol, now) or "venue_closed"
    return f"{base}:broker={b or 'unknown'}"


def session_close_at(
    broker: object,
    asset_class: object,
    symbol: str = "",
    now: datetime | None = None,
) -> datetime | None:
    """Current session close time in UTC, when known and currently open.

    This is for policy, not the execution safety gate. Unknown assets and 24/7
    venues return ``None`` so a caller never invents an end-of-day flatten for
    markets that do not have a finite close.
    """
    if not _gate_enabled():
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    b = str(getattr(broker, "value", broker) or "").strip().lower()
    smap = _broker_session_map()
    policy = smap.get(b) or smap.get("__default__", "by_asset_class")
    if policy == "always":
        return None
    if not is_tradeable(broker, asset_class, symbol, now):
        return None

    ac = _norm_ac(asset_class)
    if ac in _CRYPTO_AC:
        return None
    if ac in _FX_AC:
        # FX has one continuous weekly session ending Friday 21:00 UTC.
        days_until_friday = (4 - now.weekday()) % 7
        close_dt = (now + timedelta(days=days_until_friday)).replace(
            hour=21, minute=0, second=0, microsecond=0
        )
        if close_dt < now:
            close_dt += timedelta(days=7)
        return close_dt
    if ac in _EQUITY_AC:
        et = now.astimezone(_ET)
        close_t = time(20, 0) if _extended_hours() else time(16, 0)
        close_et = datetime.combine(et.date(), close_t, tzinfo=_ET)
        close_utc = close_et.astimezone(timezone.utc)
        return close_utc if close_utc >= now else None
    return None


def minutes_to_session_close(
    broker: object,
    asset_class: object,
    symbol: str = "",
    now: datetime | None = None,
) -> float | None:
    """Minutes until the active session closes, or ``None`` if not applicable."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    close_dt = session_close_at(broker, asset_class, symbol, now)
    if close_dt is None:
        return None
    return max(0.0, (close_dt - now).total_seconds() / 60.0)
