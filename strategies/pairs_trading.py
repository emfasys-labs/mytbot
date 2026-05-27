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
from system.dynamic_thresholds import (
    base_target_notional as dyn_base_notional,
    pairs_zscore_open_threshold,
)
from system.adaptive_regime_weights import compute_multiplier as compute_regime_multiplier


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
        # D141 — dynamic base notional.
        cfg = self.effective_config()
        try:
            static_base = Decimal(str(cfg.get("base_target_notional", "5000")))
        except (InvalidOperation, TypeError, ValueError):
            static_base = Decimal("5000")
        if static_base <= 0:
            static_base = Decimal("5000")
        live_features = cfg.get("_regime_features") or {}
        regime_mult = compute_regime_multiplier(self.name, live_features)
        dyn_base = dyn_base_notional(
            nav=cfg.get("_nav") or 0,
            strategy_net_pnl_recent=cfg.get("_strategy_pnl_recent") or 0,
            strategy_total_fills_recent=cfg.get("_strategy_fills_recent") or 0,
            regime_multiplier=regime_mult,
            quarantine_multiplier=cfg.get("_strategy_quarantine_mult") or 1,
            static_notional=static_base,
        )
        base_notional = dyn_base if dyn_base > 0 else static_base
        conf_scale = Decimal(str(max(0.8, min(1.4, 0.75 + confidence * 0.70))))
        z_scale = Decimal(str(max(0.9, min(1.5, 0.90 + z_abs * 0.20))))
        target = (base_notional * conf_scale * z_scale).quantize(Decimal("0.01"))
        return {
            "target_notional": str(target),
            "sizing_base_notional": str(base_notional.quantize(Decimal("0.01"))),
            "sizing_confidence_scale": str(conf_scale.quantize(Decimal("0.0001"))),
            "sizing_pairs_z_scale": str(z_scale.quantize(Decimal("0.0001"))),
            "sizing_regime_mult": str(regime_mult),
            "sizing_quarantine_mult": str(cfg.get("_strategy_quarantine_mult") or "1"),
            "strategy_quarantine_state": str(cfg.get("_strategy_quarantine_state") or "normal"),
            "sizing_intent_source": "pairs_spread_zscore_dyn",
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

        # D141 — cointegration health = 1 - (spread autocorrelation at lag 1).
        # Strong mean-reversion (low autocorr) → high health → tighter z gate.
        # Weak / drifting spread → high autocorr → low health → wider gate.
        try:
            spread_lag = spread.shift(1).dropna()
            spread_now = spread.iloc[1:]
            if len(spread_lag) == len(spread_now) and len(spread_lag) > 5:
                autocorr = float(np.corrcoef(spread_now.values, spread_lag.values)[0, 1])
                if not np.isfinite(autocorr):
                    autocorr = 0.0
            else:
                autocorr = 0.0
        except Exception:  # noqa: BLE001
            autocorr = 0.0
        coint_health = max(0.0, min(1.0, 1.0 - abs(autocorr)))
        z_open_dyn = float(pairs_zscore_open_threshold(
            cointegration_health=coint_health,
            static_threshold=z_open,
        ))
        if abs(z) < z_open_dyn:
            return None
        z_open = z_open_dyn

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
                "pair_cointegration_health": round(coint_health, 6),
                "pair_zscore_open_dyn": z_open,
                **md,
            },
        )
