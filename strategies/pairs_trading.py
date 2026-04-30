"""
Cointegration-lite pairs strategy.

Uses spread z-score around a rolling hedge ratio (OLS beta approximation)
to emit relative-value mean-reversion signals.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import numpy as np
import pandas as pd

from signals.engine import RawSignal


class PairsTradingStrategy:
    name = "pairs_trading"
    preferred_broker = "ibkr"
    asset_class = "equity"

    def __init__(self, config: dict[str, Any]):
        self.config = dict(config or {})
        self.enabled = bool(self.config.get("enabled", False))

    def effective_config(self) -> dict[str, Any]:
        cfg = dict(self.config or {})
        mode = str(cfg.get("_active_profile_mode") or "").strip().lower()
        mode_cfg = cfg.get("mode_calibration", {}) or {}
        if mode and isinstance(mode_cfg, dict):
            override = mode_cfg.get(mode)
            if isinstance(override, dict):
                cfg.update(override)
        return cfg

    def _compute_target_notional(self, confidence: float, z_abs: float) -> dict[str, str]:
        cfg = self.effective_config()
        try:
            base_notional = Decimal(str(cfg.get("base_target_notional", "5000")))
        except (InvalidOperation, TypeError, ValueError):
            base_notional = Decimal("5000")
        if base_notional <= 0:
            base_notional = Decimal("5000")
        conf_scale = Decimal(str(max(0.8, min(1.4, 0.75 + confidence * 0.70))))
        z_scale = Decimal(str(max(0.9, min(1.5, 0.90 + z_abs * 0.20))))
        target = (base_notional * conf_scale * z_scale).quantize(Decimal("0.01"))
        return {
            "target_notional": str(target),
            "sizing_base_notional": str(base_notional.quantize(Decimal("0.01"))),
            "sizing_confidence_scale": str(conf_scale.quantize(Decimal("0.0001"))),
            "sizing_pairs_z_scale": str(z_scale.quantize(Decimal("0.0001"))),
            "sizing_intent_source": "pairs_spread_zscore",
        }

    def generate_signals(self, feature_map: dict[str, pd.DataFrame]) -> list[RawSignal]:
        if not self.enabled:
            return []
        cfg = self.effective_config()
        pairs = cfg.get("pairs") or []
        out: list[RawSignal] = []
        lookback = int(cfg.get("lookback_bars", 90))
        z_open = float(cfg.get("zscore_open", 2.0))

        for pair in pairs:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            a, b = str(pair[0]).strip().upper(), str(pair[1]).strip().upper()
            dfa = feature_map.get(a)
            dfb = feature_map.get(b)
            if dfa is None or dfb is None or dfa.empty or dfb.empty:
                continue
            sig = self._pair_signal(a, b, dfa, dfb, lookback=lookback, z_open=z_open)
            if sig is not None:
                out.append(sig)
        return out

    def _pair_signal(
        self,
        a: str,
        b: str,
        dfa: pd.DataFrame,
        dfb: pd.DataFrame,
        *,
        lookback: int,
        z_open: float,
    ) -> Optional[RawSignal]:
        if "close" not in dfa.columns or "close" not in dfb.columns:
            return None
        sa = pd.to_numeric(dfa["close"], errors="coerce").dropna().tail(lookback)
        sb = pd.to_numeric(dfb["close"], errors="coerce").dropna().tail(lookback)
        if len(sa) < max(40, lookback // 2) or len(sb) < max(40, lookback // 2):
            return None
        idx = sa.index.intersection(sb.index)
        if len(idx) < max(35, lookback // 2):
            return None
        xa = sa.loc[idx]
        xb = sb.loc[idx]
        var_b = float(np.var(xb.values))
        if var_b <= 0:
            return None
        beta = float(np.cov(xa.values, xb.values)[0, 1] / var_b)
        spread = xa - beta * xb
        std = float(spread.std())
        if std <= 0:
            return None
        z = float((spread.iloc[-1] - spread.mean()) / std)
        if abs(z) < z_open:
            return None

        # Long the undervalued leg, short the overvalued leg.
        # Emit one leg per cycle (allocator can still combine with other opportunities).
        side = "sell" if z > 0 else "buy"
        z_abs = abs(z)
        confidence = min(0.58 + max(0.0, z_abs - z_open) * 0.12, 0.93)
        md = self._compute_target_notional(confidence=confidence, z_abs=z_abs)
        pair_symbol = a
        return RawSignal(
            strategy=self.name,
            symbol=pair_symbol,
            side=side,
            confidence=float(confidence),
            broker=self.preferred_broker,
            asset_class=self.asset_class,
            metadata={
                "pair_primary": a,
                "pair_hedge": b,
                "pair_beta": round(beta, 6),
                "pair_spread_z": round(z, 6),
                "pair_side_note": f"{a} {'short' if side == 'sell' else 'long'} vs {b}",
                **md,
            },
        )
