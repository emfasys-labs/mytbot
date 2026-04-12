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
