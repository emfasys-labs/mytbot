"""Shared helpers for the orchestrator trading loop (YAML, volume z-score, mode profile)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

_CRYPTO_SUFFIXES = ("-USD", "-USDT", "-EUR", "-GBP", "/USD", "/USDT", "/EUR", "/GBP")
_CRYPTO_BASES = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "DOT", "MATIC", "LINK", "UNI", "LTC"}


def is_crypto_symbol(symbol: str) -> bool:
    s = symbol.upper().strip()
    if any(s.endswith(suf) for suf in _CRYPTO_SUFFIXES):
        base = s.split("-")[0].split("/")[0]
        if base in _CRYPTO_BASES:
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

    if s.endswith("=X") or s.endswith("=F"):
        s = s[:-2]

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


def apply_saved_mode_to_risk_cfg(risk_engine: Any) -> None:
    import json as _json

    mode_file = Path("data/runtime/active_mode.json")
    if not mode_file.is_file():
        return
    try:
        mode = _json.loads(mode_file.read_text(encoding="utf-8")).get("mode", "trader")
    except Exception:  # noqa: BLE001
        return
    modes = load_yaml("config/risk_modes.yaml")
    profile = modes.get(mode, {})
    if risk_engine.config.get("allocator_d015_primary"):
        for key in ("label", "description"):
            if key in profile:
                risk_engine.config[key] = profile[key]
        if profile:
            logger.info("trading_loop | applied mode labels only (D015 primary) | mode={}", mode)
        return
    for key, value in profile.items():
        if key in ("label", "description"):
            continue
        risk_engine.config[key] = value
    if profile:
        logger.info("trading_loop | applied mode profile | mode={}", mode)


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
