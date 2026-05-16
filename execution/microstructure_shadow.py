"""Phase D execution-quality microstructure shadow.

This module is intentionally inert: it computes diagnostics from the current
order book and returns metadata for audit. It never blocks, reroutes, resizes,
or mutates orders by itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from brokers.base import OrderBook
from data.orderbook_features import (
    OrderbookLevel,
    OrderbookSnapshot,
    build_orderbook_features,
)

MICROSTRUCTURE_DEFAULT_PATH = Path("config/microstructure.yaml")


@dataclass(frozen=True)
class MicrostructureShadowConfig:
    enabled: bool = False
    asset_classes: tuple[str, ...] = ("crypto",)
    depth: int = 5
    max_spread_bps: float = 25.0
    max_vpin_proxy: float = 0.70
    max_liquidity_fragility: float = 2.50
    max_quote_staleness_seconds: float = 5.0


def load_microstructure_shadow_config(path: Path = MICROSTRUCTURE_DEFAULT_PATH) -> MicrostructureShadowConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return MicrostructureShadowConfig()
    cfg = raw.get("microstructure") or {}
    shadow = cfg.get("shadow") or {}
    return MicrostructureShadowConfig(
        enabled=bool(shadow.get("enabled", False)),
        asset_classes=tuple(str(x).strip().lower() for x in (cfg.get("asset_classes") or ["crypto"]) if str(x).strip()),
        depth=max(1, int(shadow.get("depth", cfg.get("depth", 5)))),
        max_spread_bps=float(shadow.get("max_spread_bps", 25.0)),
        max_vpin_proxy=float(shadow.get("max_vpin_proxy", 0.70)),
        max_liquidity_fragility=float(shadow.get("max_liquidity_fragility", 2.50)),
        max_quote_staleness_seconds=float(shadow.get("max_quote_staleness_seconds", cfg.get("max_staleness_seconds", 5.0))),
    )


def _parse_ts(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        text = str(raw)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def snapshot_from_order_book(order_book: OrderBook, *, asset_class: str = "crypto") -> OrderbookSnapshot:
    return OrderbookSnapshot(
        symbol=str(order_book.symbol),
        timestamp=_parse_ts(getattr(order_book, "timestamp", None)),
        asset_class=str(asset_class or "other"),
        bids=tuple(OrderbookLevel(price=Decimal(str(p)), quantity=Decimal(str(q))) for p, q in (order_book.bids or [])),
        asks=tuple(OrderbookLevel(price=Decimal(str(p)), quantity=Decimal(str(q))) for p, q in (order_book.asks or [])),
    )


def score_microstructure_shadow(
    features: dict[str, Any],
    *,
    cfg: MicrostructureShadowConfig | None = None,
) -> dict[str, Any]:
    c = cfg or MicrostructureShadowConfig(enabled=True)
    reasons: list[str] = []
    risk = 0.0

    spread = features.get("spread_bps")
    if spread is None:
        reasons.append("missing_spread")
        risk += 0.25
    else:
        spread_f = float(spread)
        risk += min(0.35, max(0.0, spread_f / max(c.max_spread_bps, 1e-9)) * 0.35)
        if spread_f > c.max_spread_bps:
            reasons.append("wide_spread")

    vpin = features.get("vpin_proxy")
    if vpin is not None:
        vpin_f = abs(float(vpin))
        risk += min(0.25, vpin_f * 0.25)
        if vpin_f > c.max_vpin_proxy:
            reasons.append("toxic_depth_imbalance")

    frag = features.get("liquidity_fragility")
    if frag is not None:
        frag_f = float(frag)
        risk += min(0.20, max(0.0, frag_f / max(c.max_liquidity_fragility, 1e-9)) * 0.20)
        if frag_f > c.max_liquidity_fragility:
            reasons.append("fragile_liquidity")

    stale = features.get("quote_staleness")
    if stale is not None:
        stale_f = float(stale)
        risk += min(0.20, max(0.0, stale_f / max(c.max_quote_staleness_seconds, 1e-9)) * 0.20)
        if stale_f > c.max_quote_staleness_seconds:
            reasons.append("stale_quote")

    if float(features.get("well_formed") or 0.0) < 1.0:
        reasons.append("malformed_book")
        risk = max(risk, 0.75)

    risk = max(0.0, min(1.0, risk))
    label = "high_risk" if risk >= 0.70 else "caution" if risk >= 0.35 else "normal"
    return {
        "microstructure_shadow_used": True,
        "microstructure_shadow_risk": round(risk, 6),
        "microstructure_shadow_label": label,
        "microstructure_shadow_reasons": ",".join(dict.fromkeys(reasons)),
    }


async def build_microstructure_shadow_metadata(
    *,
    broker: Any,
    symbol: str,
    asset_class: str,
    cfg: MicrostructureShadowConfig | None = None,
) -> dict[str, Any]:
    c = cfg or load_microstructure_shadow_config()
    if not c.enabled:
        return {"microstructure_shadow_used": False, "microstructure_shadow_reason": "disabled"}
    ac = str(asset_class or "other").strip().lower()
    if ac not in c.asset_classes:
        return {"microstructure_shadow_used": False, "microstructure_shadow_reason": "asset_class_not_enabled"}
    if broker is None or not hasattr(broker, "get_order_book"):
        return {"microstructure_shadow_used": False, "microstructure_shadow_reason": "broker_unavailable"}
    try:
        book = await broker.get_order_book(symbol, depth=c.depth)
        snap = snapshot_from_order_book(book, asset_class=ac)
        features = build_orderbook_features(snap, depth=c.depth)
        score = score_microstructure_shadow(features, cfg=c)
    except Exception as exc:  # noqa: BLE001
        return {
            "microstructure_shadow_used": False,
            "microstructure_shadow_reason": "fetch_or_score_failed",
            "microstructure_shadow_error": str(exc)[:160],
        }
    out: dict[str, Any] = {
        **score,
        "microstructure_shadow_depth": c.depth,
    }
    for key, value in features.items():
        if value is not None and isinstance(value, (int, float)):
            out[f"microstructure_{key}"] = round(float(value), 6)
    return out
