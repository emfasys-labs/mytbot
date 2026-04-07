from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from data.symbols import canonical_trigger_symbol


@dataclass
class DependencyOpportunity:
    symbol: str
    asset_class: str
    direction: str
    trigger_symbol: str
    trigger_move_pct: float
    static_confidence: float
    live_correlation: float
    blended_confidence: float
    expected_lag_hours: int
    thesis: str
    confirmed_by_signals: bool = False
    risk_approved: bool = False


class DependencyGraphEngine:
    MIN_CONFIDENCE_THRESHOLD = 0.45

    def __init__(
        self,
        relationships_path: str = "graph/data/relationships.yaml",
        correlation_engine=None,
    ) -> None:
        self.correlation_engine = correlation_engine
        self._relationships_path = relationships_path
        self._relationships = self._load_relationships(relationships_path)
        logger.info(
            "DependencyGraphEngine loaded | sets={} | path={}",
            len(self._relationships),
            relationships_path,
        )

    def get_opportunities(self, anomaly) -> list[DependencyOpportunity]:
        opportunities: list[DependencyOpportunity] = []
        matches = self._find_relationships(anomaly.symbol, anomaly.direction)
        for rel in matches:
            live_corr = self._get_live_correlation(anomaly.symbol, rel["instrument"])
            static_conf = float(rel["confidence"])
            blended = (static_conf * 0.40) + (live_corr * 0.60)
            magnitude_scalar = min(abs(float(anomaly.price_z_score)) / 2.0, 2.0)
            blended = min(blended * magnitude_scalar, 0.95)
            if blended < self.MIN_CONFIDENCE_THRESHOLD:
                continue
            opportunities.append(
                DependencyOpportunity(
                    symbol=str(rel["instrument"]),
                    asset_class=str(rel["asset_class"]),
                    direction=str(rel["direction"]),
                    trigger_symbol=str(anomaly.symbol),
                    trigger_move_pct=float(anomaly.price_move_pct),
                    static_confidence=static_conf,
                    live_correlation=float(live_corr),
                    blended_confidence=round(float(blended), 4),
                    expected_lag_hours=int(rel.get("lag_hours", 0)),
                    thesis=str(rel.get("mechanism", "")),
                )
            )
        opportunities.sort(key=lambda x: x.blended_confidence, reverse=True)
        return opportunities

    def get_full_chain(self, anomaly, depth: int = 2) -> dict[str, Any]:
        chain: dict[str, Any] = {
            "trigger": anomaly.symbol,
            "direction": anomaly.direction,
            "level_1": self.get_opportunities(anomaly),
            "level_2": [],
        }
        if depth < 2:
            return chain
        seen = {str(anomaly.symbol).upper()}
        for l1 in chain["level_1"][:5]:
            key = str(l1.symbol).upper()
            if key in seen:
                continue
            seen.add(key)
            synthetic = self._synthetic_anomaly(anomaly, l1)
            l2 = self.get_opportunities(synthetic)
            chain["level_2"].extend([o for o in l2 if str(o.symbol).upper() not in seen][:3])
        return chain

    def _synthetic_anomaly(self, base, l1):
        class _A:
            pass

        a = _A()
        a.symbol = l1.symbol
        a.asset_class = l1.asset_class
        a.timestamp = getattr(base, "timestamp", "")
        a.price_move_pct = float(base.price_move_pct) * float(l1.static_confidence)
        a.price_z_score = float(base.price_z_score) * float(l1.static_confidence)
        a.volume_ratio = 1.0
        a.volume_z_score = 0.0
        a.news_velocity = 1.0
        a.news_sentiment = 0.0
        a.anomaly_score = float(l1.blended_confidence)
        a.direction = l1.direction
        a.triggered_by = base.symbol
        return a

    def _find_relationships(self, symbol: str, direction: str) -> list[dict[str, Any]]:
        trig = canonical_trigger_symbol(symbol)
        k = f"{trig}_{direction}"
        if k in self._relationships:
            return self._relationships[k]
        return []

    def _get_live_correlation(self, trigger: str, target: str) -> float:
        if self.correlation_engine is None:
            return 1.0
        try:
            return float(self.correlation_engine.get_correlation(trigger, target))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Correlation lookup failed {} / {} | {}", trigger, target, exc)
            return 0.70

    def _load_relationships(self, path: str) -> dict[str, list[dict[str, Any]]]:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"relationships file not found: {path}")
        with p.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        rows = data.get("relationships")
        if not isinstance(rows, list):
            raise ValueError("relationships.yaml must contain top-level list field 'relationships'")
        out: dict[str, list[dict[str, Any]]] = {}
        for i, row in enumerate(rows):
            self._validate_relationship_row(i, row)
            key = f"{str(row['trigger']).lower()}_{str(row['direction'])}"
            out.setdefault(key, []).extend(row["impacts"])
        return out

    def _validate_relationship_row(self, idx: int, row: Any) -> None:
        if not isinstance(row, dict):
            raise ValueError(f"relationships[{idx}] must be object")
        for k in ("trigger", "direction", "impacts"):
            if k not in row:
                raise ValueError(f"relationships[{idx}] missing key: {k}")
        if not isinstance(row["impacts"], list) or not row["impacts"]:
            raise ValueError(f"relationships[{idx}].impacts must be non-empty list")
        for j, imp in enumerate(row["impacts"]):
            if not isinstance(imp, dict):
                raise ValueError(f"relationships[{idx}].impacts[{j}] must be object")
            for k in ("instrument", "asset_class", "direction", "confidence", "lag_hours", "mechanism"):
                if k not in imp:
                    raise ValueError(f"relationships[{idx}].impacts[{j}] missing key: {k}")
            c = float(imp["confidence"])
            if c < 0.0 or c > 1.0:
                raise ValueError(f"relationships[{idx}].impacts[{j}] confidence out of range: {c}")
            int(imp["lag_hours"])

    def add_learned_relationship(
        self,
        trigger: str,
        direction: str,
        target: str,
        target_direction: str,
        confidence: float,
        mechanism: str,
        lag_hours: int,
    ) -> None:
        logger.info(
            "LEARNED RELATIONSHIP | {} {} -> {} {} | conf={} | lag={}h | {}",
            trigger,
            direction,
            target,
            target_direction,
            confidence,
            lag_hours,
            mechanism,
        )
