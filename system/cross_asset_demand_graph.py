"""
Cross-asset demand graph signal extraction.

Models directional demand via anchored relationships:
- pro-demand anchors (equities, cyclicals, crypto)
- defensive anchors (bonds, dollar proxies, gold)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass
class CrossAssetDemandResult:
    score: float
    market_volatility: float
    coverage: float


class CrossAssetDemandGraph:
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = dict(config or {})

    @staticmethod
    def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
        return max(lo, min(hi, float(x)))

    @staticmethod
    def _ret(df: pd.DataFrame) -> float | None:
        if df is None or df.empty or "close" not in df.columns or len(df) < 2:
            return None
        try:
            c0 = float(df["close"].iloc[-2])
            c1 = float(df["close"].iloc[-1])
        except Exception:  # noqa: BLE001
            return None
        if c0 <= 0:
            return None
        return (c1 - c0) / c0

    def evaluate(self, feature_map: dict[str, pd.DataFrame]) -> CrossAssetDemandResult:
        if not feature_map:
            return CrossAssetDemandResult(score=0.0, market_volatility=0.0, coverage=0.0)

        risk_on = [str(x).strip().upper() for x in self.config.get("risk_on_anchors", ["SPY", "QQQ", "XLE", "BTC-USD"])]
        risk_off = [str(x).strip().upper() for x in self.config.get("risk_off_anchors", ["TLT", "GLD", "DXY"])]
        fm = {str(k).strip().upper(): v for k, v in feature_map.items()}

        on_rets = [r for s in risk_on if s in fm and (r := self._ret(fm[s])) is not None]
        off_rets = [r for s in risk_off if s in fm and (r := self._ret(fm[s])) is not None]
        covered = len(on_rets) + len(off_rets)
        total = max(1, len(risk_on) + len(risk_off))
        coverage = covered / total
        if not on_rets and not off_rets:
            return CrossAssetDemandResult(score=0.0, market_volatility=0.0, coverage=coverage)

        on_mean = sum(on_rets) / len(on_rets) if on_rets else 0.0
        off_mean = sum(off_rets) / len(off_rets) if off_rets else 0.0
        spread = on_mean - off_mean

        # Optional explicit relationship graph:
        # each edge: [src, dst, sign] where sign>0 means co-move supportive,
        # sign<0 means inverse relation (src up while dst down is pro-demand).
        edge_terms: list[float] = []
        for e in self.config.get("graph_edges", []) or []:
            if not isinstance(e, (list, tuple)) or len(e) < 3:
                continue
            src = str(e[0]).strip().upper()
            dst = str(e[1]).strip().upper()
            try:
                sign = float(e[2])
            except (TypeError, ValueError):
                sign = 0.0
            if src not in fm or dst not in fm or abs(sign) < 1e-9:
                continue
            rs = self._ret(fm[src])
            rd = self._ret(fm[dst])
            if rs is None or rd is None:
                continue
            edge_terms.append(sign * (rs - rd))
        if edge_terms:
            spread = 0.7 * spread + 0.3 * (sum(edge_terms) / len(edge_terms))

        # Market volatility proxy from absolute anchor moves.
        abs_moves = [abs(x) for x in on_rets + off_rets]
        vol = sum(abs_moves) / len(abs_moves) if abs_moves else 0.0

        scale = float(self.config.get("cross_asset_scale", 30.0))
        score = self._clip(spread * scale)
        return CrossAssetDemandResult(score=score, market_volatility=max(0.0, vol), coverage=coverage)
