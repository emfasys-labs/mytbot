"""Optional AI-fusion shadow logging (Phase A) — no orders, no live effect.

Mirrors ``system/d015_shadow.py`` exactly in shape and safety:
  * gated by its OWN env var ``FUSION_SHADOW`` (default OFF) — NOT tied to
    the already-on meta-labeller / d015 flags;
  * fully wrapped in try/except → ``logger.debug`` so it can never disturb
    the live path;
  * emits a single ``logger.info("fusion_shadow | {json}")`` line and
    nothing else — no DB writes, no metadata mutation, no model calls.

It reads values the live pipeline already computed/stamped on the signal's
metadata, runs them through the uniform fusion contract, and logs the fused
view alongside the live decision so the two can be compared offline before
fusion ever influences anything.
"""

from __future__ import annotations

import json
import os
from typing import Any

from loguru import logger

from signals.fusion import build_fusion_inputs_from_metadata, fuse


def fusion_shadow_enabled() -> bool:
    v = os.getenv("FUSION_SHADOW", "").strip().lower()
    return v in ("1", "true", "yes", "on")


async def log_fusion_shadow_for_signal(
    *,
    symbol: str,
    side: str,
    confidence: float,
    metadata: dict[str, Any],
    mode: str | None = None,
) -> None:
    """Shadow-log the fused evidence view vs the live decision. No-op unless
    ``FUSION_SHADOW`` is enabled. Exception-safe by construction."""
    if not fusion_shadow_enabled():
        return
    try:
        fi = build_fusion_inputs_from_metadata(
            symbol=symbol,
            side=side,
            base_confidence=float(confidence),
            metadata=metadata or {},
        )
        ev = fuse(fi)
        md = metadata or {}
        payload = {
            "symbol": symbol,
            "side": side,
            "mode": mode,
            "regime": ev.regime_label,
            # Live decision references (already computed upstream):
            "live_confidence": round(float(confidence), 6),
            "live_meta_label_prob": md.get("meta_label_probability"),
            "live_meta_label_kept": md.get("meta_label_kept"),
            "live_expected_edge": md.get("expected_edge"),
            "live_forecast_used": md.get("forecast_used"),
            # Fused (shadow) view:
            "fused_direction": round(ev.combined_direction, 6),
            "fused_expected_edge_bps": round(ev.combined_expected_edge_bps, 4),
            "fused_aggregate_confidence": round(ev.aggregate_confidence, 6),
            "fused_agreement": round(ev.agreement, 4),
            "fused_dispersion": round(ev.dispersion, 4),
            "fused_meta_label_prob": ev.meta_label_probability,
            "fused_models_active": ev.contributing,
            "fused_n_models": ev.n_models,
            "fused_n_fallback": ev.n_fallback,
            "fused_notes": ev.notes,
        }
        logger.info("fusion_shadow | {}", json.dumps(payload, default=str))
    except Exception as exc:  # noqa: BLE001
        logger.debug("fusion_shadow | skipped | {}", exc)
