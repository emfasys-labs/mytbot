"""Shared helpers for the orchestrator trading loop (YAML, volume z-score, mode profile)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

_CRYPTO_SUFFIXES = ("-USD", "-USDT", "-USDC", "-EUR", "-GBP", "/USD", "/USDT", "/USDC", "/EUR", "/GBP")
# Retained for reference / fast-path; detection is no longer gated on it.
_CRYPTO_BASES = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "DOT", "MATIC", "LINK", "UNI", "LTC"}


def is_crypto_symbol(symbol: str) -> bool:
    """Suffix-based crypto detection (audit #9).

    The old code only returned True if the base was in a hard-coded 12-coin
    allowlist (``_CRYPTO_BASES``). Every other crypto pair the pipeline
    surfaced — ATOM-USD, NEAR-USD, API3-USD, BGB-USD, … — fell through to
    "equity" and was then routed to IBKR as a ``Stock`` contract, producing
    an Error 200 ("No security definition") and a wasted, doomed order on
    *every* loop iteration.

    The pipeline's crypto convention is ``BASE-USD`` / ``BASE/USDT`` etc.
    (forex uses the disjoint ``=X`` form, futures ``=F``). So: any symbol
    ending in a crypto quote suffix whose base is a plausible alnum ticker
    is crypto. This cannot collide with US equity dual-class tickers
    (``BRK-B``, ``BF-B`` — they don't end in a quote suffix) nor with forex
    (``EURUSD=X`` — no ``-USD`` suffix).
    """
    s = symbol.upper().strip()
    for suf in _CRYPTO_SUFFIXES:
        if s.endswith(suf):
            base = s[: -len(suf)].rstrip("-/")
            if 1 <= len(base) <= 12 and base.isalnum():
                return True
    return False


def is_forex_symbol(symbol: str) -> bool:
    """Recognise yfinance forex tickers like ``EURUSD=X``.

    We intentionally stay strict: must end ``=X`` and have a 6-letter base.
    That keeps false positives (crypto pairs, tickers with stray ``=``) out.
    """
    s = symbol.strip().upper()
    if not s.endswith("=X"):
        return False
    base = s[:-2]
    return len(base) == 6 and base.isalpha()


def is_futures_symbol(symbol: str) -> bool:
    """Recognise yfinance continuous-futures tickers like ``ES=F`` / ``CL=F``."""
    s = symbol.strip().upper()
    if not s.endswith("=F"):
        return False
    base = s[:-2]
    return 1 <= len(base) <= 4 and base.isalnum()


def asset_class_for_symbol(symbol: str) -> str:
    """Single source of truth for mapping a ticker to its asset class.

    Priority: crypto → forex → future → equity. Used by the trading loop to
    relabel every signal so the Smart Order Router can pick the right broker.
    """
    if is_crypto_symbol(symbol):
        return "crypto"
    if is_forex_symbol(symbol):
        return "forex"
    if is_futures_symbol(symbol):
        return "future"
    return "equity"


def broker_symbol_for(symbol: str, broker: str) -> str:
    """Translate a pipeline ticker (yfinance convention) to a broker-native one.

    Pipeline conventions:
      * ``EURUSD=X`` for forex (yfinance)
      * ``ES=F`` for futures (yfinance) — still execution-gated elsewhere
      * ``BTC-USD`` for crypto (yfinance)

    Broker-native conventions we translate *to*:
      * IBKR: ``EURUSD`` (6-char forex pair), ``BTC-USD`` kept as-is (IBKR
        accepts dashed crypto).
      * Alpaca crypto: ``BTC/USD`` (slash separator) — NOT ``BTC-USD``.
        Submitting the dashed form triggers ``asset "BTC-USD" not found``.
      * Bybit / Binance / Kraken: adapters already normalise crypto
        internally, so we leave them alone here.
    """
    s = (symbol or "").strip().upper()
    b = (broker or "").strip().lower()
    if not s or not b:
        return s

    # Forex: strip the yfinance ``=X`` suffix (``EURUSD=X`` → ``EURUSD``).
    if s.endswith("=X"):
        s = s[:-2]
    # Futures: KEEP the ``=F`` continuous suffix (``CL=F``). The IBKR adapter
    # resolves it to the front-month contract; the suffix is what makes the
    # symbol unambiguous vs. equity tickers that share a root (CL=Colgate),
    # and is the canonical pipeline/ledger key (D165).

    # Alpaca crypto uses ``BASE/QUOTE`` slashes, not dashes.
    # Only rewrite when the symbol clearly looks like a crypto pair
    # (``*-USD`` / ``*-USDT`` / ``*-USDC``) so equities like ``BRK-B`` are
    # not affected.
    if b == "alpaca" and "-" in s:
        base, _, quote = s.rpartition("-")
        if base and quote in {"USD", "USDT", "USDC"}:
            return f"{base}/{quote}"

    return s


def load_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def enrich_signal_volume_z(signal: Any, df: Any) -> None:
    """
    Rolling volume z-score → signal.metadata[\"volume_z_score\"] for risk quality gate.
    """
    try:
        if df is None or not hasattr(df, "empty") or df.empty:
            return
        if "volume" not in df.columns:
            return
        vol = df["volume"].dropna()
        if len(vol) < 5:
            return
        mean_v = float(vol.mean())
        std_v = float(vol.std())
        if std_v <= 0:
            return
        latest_v = float(vol.iloc[-1])
        z = (latest_v - mean_v) / std_v
        if not isinstance(getattr(signal, "metadata", None), dict):
            signal.metadata = {}
        signal.metadata["volume_z_score"] = round(z, 4)
    except Exception:  # noqa: BLE001
        pass


def enrich_signal_liquidity(signal: Any, df: Any) -> None:
    """Average daily volume + dollar volume + realised vol → signal.metadata.

    These feed the Wave 9 cost gate: ``daily_volume`` lets the impact term
    use the real square-root model and clears the unknown-liquidity penalty;
    ``daily_volatility`` keeps the impact estimate calibrated to the symbol.
    """
    try:
        if df is None or not hasattr(df, "empty") or df.empty:
            return
        if not isinstance(getattr(signal, "metadata", None), dict):
            signal.metadata = {}
        meta = signal.metadata

        if "volume" in df.columns:
            vol = df["volume"].dropna()
            if len(vol):
                window = vol.tail(20) if len(vol) >= 5 else vol
                adv = float(window.mean())
                if adv > 0:
                    meta.setdefault("daily_volume", round(adv, 4))
                    meta.setdefault("avg_daily_volume", round(adv, 4))
                    if "close" in df.columns:
                        close_tail = df["close"].dropna().tail(len(window))
                        if len(close_tail):
                            px = float(close_tail.iloc[-1])
                            if px > 0:
                                meta.setdefault(
                                    "daily_dollar_volume",
                                    round(adv * px, 2),
                                )

        if "close" in df.columns:
            close = df["close"].dropna()
            if len(close) >= 6:
                rets = close.pct_change().dropna().tail(20)
                if len(rets) >= 5:
                    vol_pct = float(rets.std())
                    if vol_pct > 0:
                        meta.setdefault("daily_volatility", round(vol_pct, 6))
    except Exception:  # noqa: BLE001
        pass


def apply_saved_mode_to_risk_cfg(risk_engine: Any) -> None:
    """Mutate ``risk_engine.config`` to reflect the saved profile mode.

    Sources, in priority order:
      1. ``config/risk_modes.yaml`` per-mode block (legacy / D015-secondary path).
      2. ``config/risk_limits.yaml`` ``mode_overrides`` block — applied only
         when ``USE_ADAPTIVE_SIZING=1``. Lets runtime mode adjust ceilings
         without bypassing the risk engine's final vetoes.

    The mode_overrides path lets the operator pivot mode at runtime and have
    the risk engine immediately honour the current business ceilings without
    restarting.
    """
    import json as _json

    mode_file = Path("data/runtime/active_mode.json")
    mode = "trader"
    if mode_file.is_file():
        try:
            mode = _json.loads(mode_file.read_text(encoding="utf-8")).get("mode", "trader")
        except Exception:  # noqa: BLE001
            mode = "trader"

    modes = load_yaml("config/risk_modes.yaml")
    profile = modes.get(mode, {})
    d015_primary = bool(risk_engine.config.get("allocator_d015_primary"))
    if d015_primary:
        for key in ("label", "description"):
            if key in profile:
                risk_engine.config[key] = profile[key]
        if profile:
            logger.info("trading_loop | applied mode labels only (D015 primary) | mode={}", mode)
    else:
        for key, value in profile.items():
            if key in ("label", "description"):
                continue
            risk_engine.config[key] = value
        if profile:
            logger.info("trading_loop | applied mode profile | mode={}", mode)

    # Adaptive mode overrides — applied AFTER the legacy profile so they win
    # for the keys they specify (max_position_pct, max_concentration_pct,
    # max_gross_exposure_pct, asset-class buckets).
    adaptive_on = os.getenv("USE_ADAPTIVE_SIZING", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if not adaptive_on:
        return
    overrides = (risk_engine.config.get("mode_overrides") or {}).get(mode) or {}
    if not isinstance(overrides, dict) or not overrides:
        return
    applied: list[str] = []
    for key, value in overrides.items():
        risk_engine.config[key] = value
        applied.append(key)
    if applied:
        logger.info(
            "trading_loop | applied adaptive mode_overrides | mode={} keys={}",
            mode,
            applied,
        )


def d015_legacy_fallback() -> bool:
    return os.getenv("ALLOCATOR_D015_LEGACY_FALLBACK", "").strip().lower() in ("1", "true", "yes", "on")


def enrich_candidate_volume_z(candidate: Any, df: Any) -> None:
    try:
        if df is None or not hasattr(df, "empty") or df.empty:
            return
        if "volume" not in df.columns:
            return
        vol = df["volume"].dropna()
        if len(vol) < 5:
            return
        mean_v = float(vol.mean())
        std_v = float(vol.std())
        if std_v <= 0:
            return
        latest_v = float(vol.iloc[-1])
        z = (latest_v - mean_v) / std_v
        if not isinstance(getattr(candidate, "metadata", None), dict):
            candidate.metadata = {}
        candidate.metadata["volume_z_score"] = round(z, 4)
    except Exception:  # noqa: BLE001
        pass


def attach_forecast_sequence_history(
    candidate: Any, df: Any, *, enabled: bool, max_rows: int = 256
) -> None:
    """Phase B: stash a recent numeric-feature HISTORY on the candidate so
    the (sync, pure) forecast bridge can build a contract-aligned sequence
    window for a TCN member — without any DB I/O in the hot path.

    Gated: when ``enabled`` is False (the default — no sequence forecast
    member configured/enabled, which is the normal state) this returns
    immediately with **zero overhead**. The bridge selects the artefact's
    own feature columns/window from this history, so providing extra
    columns/rows here is safe and contract-robust. Never raises.
    """
    if not enabled:
        return
    try:
        if df is None or not hasattr(df, "empty") or df.empty:
            return
        num = df.select_dtypes(include="number")
        if num is None or num.empty or num.shape[0] < 2:
            return
        cols = sorted(str(c) for c in num.columns)  # deterministic order
        tail = num[cols].tail(int(max_rows))
        rows = [[float(v) for v in r] for r in tail.to_numpy().tolist()]
        if not isinstance(getattr(candidate, "metadata", None), dict):
            candidate.metadata = {}
        candidate.metadata["forecast_sequence_window"] = {
            "columns": cols,
            "rows": rows,
        }
    except Exception:  # noqa: BLE001
        pass


def enrich_candidate_liquidity(candidate: Any, df: Any) -> None:
    """Mirror of :func:`enrich_signal_liquidity` for batch candidates."""
    try:
        if df is None or not hasattr(df, "empty") or df.empty:
            return
        if not isinstance(getattr(candidate, "metadata", None), dict):
            candidate.metadata = {}
        meta = candidate.metadata
        if "volume" in df.columns:
            vol = df["volume"].dropna()
            if len(vol):
                window = vol.tail(20) if len(vol) >= 5 else vol
                adv = float(window.mean())
                if adv > 0:
                    meta.setdefault("daily_volume", round(adv, 4))
                    meta.setdefault("avg_daily_volume", round(adv, 4))
                    if "close" in df.columns:
                        close_tail = df["close"].dropna().tail(len(window))
                        if len(close_tail):
                            px = float(close_tail.iloc[-1])
                            if px > 0:
                                meta.setdefault(
                                    "daily_dollar_volume",
                                    round(adv * px, 2),
                                )
        if "close" in df.columns:
            close = df["close"].dropna()
            if len(close) >= 6:
                rets = close.pct_change().dropna().tail(20)
                if len(rets) >= 5:
                    vol_pct = float(rets.std())
                    if vol_pct > 0:
                        meta.setdefault("daily_volatility", round(vol_pct, 6))
    except Exception:  # noqa: BLE001
        pass
