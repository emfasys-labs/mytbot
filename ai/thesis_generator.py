from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from loguru import logger


@dataclass
class InvestmentThesis:
    trigger_symbol: str
    trigger_direction: str
    trigger_explanation: str
    priority_opportunities: list[dict]
    overall_confidence: float
    time_horizon_hours: int
    invalidation_conditions: list[str]
    generated_at: str
    model_used: str
    tokens_used: int


class ThesisGenerator:
    MIN_ANOMALY_SCORE_FOR_AI = 0.65
    CACHE_MINUTES = 30

    SYSTEM_PROMPT = """You are a senior macro analyst and quantitative trader.
Return strict JSON only following the requested schema."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self._cache: dict[str, tuple[InvestmentThesis, datetime]] = {}

    async def generate(self, anomaly, opportunities: list, market_context: Optional[dict] = None) -> Optional[InvestmentThesis]:
        if anomaly.anomaly_score < self.MIN_ANOMALY_SCORE_FOR_AI:
            return None
        key = f"{anomaly.symbol}_{anomaly.direction}_{anomaly.timestamp[:13]}"
        now = datetime.now(timezone.utc)
        cached = self._cache.get(key)
        if cached is not None:
            thesis, ts = cached
            if (now - ts).total_seconds() <= (self.CACHE_MINUTES * 60):
                return thesis
        if not self.api_key:
            thesis = self._stub_thesis(anomaly, opportunities)
            self._cache[key] = (thesis, now)
            return thesis
        try:
            thesis = await self._call_claude(anomaly, opportunities, market_context or {})
            self._cache[key] = (thesis, now)
            return thesis
        except Exception as exc:  # noqa: BLE001
            logger.warning("Thesis generation failed; fallback to stub | {}", exc)
            thesis = self._stub_thesis(anomaly, opportunities)
            self._cache[key] = (thesis, now)
            return thesis

    def _stub_thesis(self, anomaly, opportunities: list) -> InvestmentThesis:
        pr = [
            {
                "symbol": o.symbol,
                "direction": o.direction,
                "rationale": o.thesis,
                "confidence": o.blended_confidence,
                "time_horizon_hours": o.expected_lag_hours or 4,
                "entry_note": "Standard dependency relationship",
            }
            for o in opportunities[:5]
        ]
        return InvestmentThesis(
            trigger_symbol=anomaly.symbol,
            trigger_direction=anomaly.direction,
            trigger_explanation=(
                f"{anomaly.symbol} moved {anomaly.price_move_pct:+.2f}% "
                f"({anomaly.price_z_score:.1f} standard deviations). "
                f"Dependency graph found {len(opportunities)} opportunities."
            ),
            priority_opportunities=pr,
            overall_confidence=min(float(anomaly.anomaly_score) * 0.8, 0.85),
            time_horizon_hours=24,
            invalidation_conditions=[
                f"{anomaly.symbol} reverses the anomalous move",
                "Volume drops significantly (false breakout)",
                "Contradicting macro news released",
            ],
            generated_at=anomaly.timestamp,
            model_used="stub_dependency_graph",
            tokens_used=0,
        )

    async def _call_claude(self, anomaly, opportunities: list, context: dict) -> InvestmentThesis:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=self.api_key)
        user_message = f"""
Market anomaly detected:
- Instrument: {anomaly.symbol} ({anomaly.asset_class})
- Move: {anomaly.price_move_pct:+.2f}%
- Statistical significance: {anomaly.price_z_score:.1f} standard deviations
- Volume: {anomaly.volume_ratio:.1f}x average ({anomaly.volume_z_score:.1f}σ)
- News velocity: {anomaly.news_velocity:.1f}x baseline
- News sentiment: {anomaly.news_sentiment:+.2f}
- Direction: {anomaly.direction}

Dependency opportunities:
{self._format_opportunities(opportunities)}

Market context:
{json.dumps(context, indent=2)}
"""
        msg = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=self.SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        raw = msg.content[0].text
        data = json.loads(raw)
        return InvestmentThesis(
            trigger_symbol=anomaly.symbol,
            trigger_direction=anomaly.direction,
            trigger_explanation=str(data["trigger_explanation"]),
            priority_opportunities=list(data["priority_opportunities"]),
            overall_confidence=float(data["overall_confidence"]),
            time_horizon_hours=int(data["time_horizon_hours"]),
            invalidation_conditions=list(data["invalidation_conditions"]),
            generated_at=datetime.now(timezone.utc).isoformat(),
            model_used="claude-sonnet-4-6",
            tokens_used=int(msg.usage.input_tokens + msg.usage.output_tokens),
        )

    def _format_opportunities(self, opportunities: list) -> str:
        lines = []
        for o in opportunities[:8]:
            lines.append(
                f"- {o.symbol} ({o.asset_class}): {o.direction}, conf={o.blended_confidence:.2f}, "
                f"lag={o.expected_lag_hours}h, reason={o.thesis}"
            )
        return "\n".join(lines)
