from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
from loguru import logger
from sqlalchemy import desc, select

from storage.models import AIOutputLog, FeatureSnapshot, NewsHeadline


@dataclass
class AnomalySignal:
    symbol: str
    asset_class: str
    timestamp: str
    price_move_pct: float
    price_z_score: float
    volume_ratio: float
    volume_z_score: float
    news_velocity: float
    news_sentiment: float
    anomaly_score: float
    direction: str
    sector: Optional[str] = None
    region: Optional[str] = None
    triggered_by: Optional[str] = None

    def is_significant(self, threshold: float = 2.0) -> bool:
        return abs(self.price_z_score) >= threshold


class UniverseScanner:
    PRICE_Z_THRESHOLD = 2.0
    LOOKBACK_PERIODS = 252

    def __init__(self, universe, session_factory, *, cooldown_seconds: int = 300):
        self.universe = universe
        self.session_factory = session_factory
        self.cooldown_seconds = max(0, int(cooldown_seconds))
        self._last_seen: dict[str, datetime] = {}
        logger.info("UniverseScanner initialised")

    async def scan(self) -> list[AnomalySignal]:
        anomalies: list[AnomalySignal] = []
        for instrument in self.universe.get_all():
            if not instrument.scan_enabled:
                continue
            if self._is_cooldown(instrument.symbol):
                continue
            try:
                a = await self._scan_instrument(instrument)
            except Exception as exc:  # noqa: BLE001
                logger.error("Scanner error on {}: {}", instrument.symbol, exc)
                continue
            if a and a.is_significant(self.PRICE_Z_THRESHOLD):
                anomalies.append(a)
                self._last_seen[instrument.symbol] = datetime.now(timezone.utc)
        anomalies.sort(key=lambda x: x.anomaly_score, reverse=True)
        return anomalies

    def _is_cooldown(self, symbol: str) -> bool:
        if self.cooldown_seconds <= 0:
            return False
        last = self._last_seen.get(symbol)
        if last is None:
            return False
        return (datetime.now(timezone.utc) - last).total_seconds() < self.cooldown_seconds

    async def _scan_instrument(self, instrument) -> Optional[AnomalySignal]:
        async with self.session_factory() as session:
            q = await session.execute(
                select(FeatureSnapshot)
                .where(FeatureSnapshot.symbol == instrument.symbol)
                .where(FeatureSnapshot.timeframe.in_(["1d", "1h"]))
                .order_by(desc(FeatureSnapshot.bar_timestamp))
                .limit(self.LOOKBACK_PERIODS + 2)
            )
            rows = list(q.scalars().all())
            if len(rows) < 30:
                return None

            rows.reverse()
            closes = [float(r.close) for r in rows]
            volumes = [float(r.volume) for r in rows]
            returns = []
            for i in range(1, len(closes)):
                prev = closes[i - 1]
                cur = closes[i]
                if prev <= 0:
                    continue
                returns.append(((cur - prev) / prev) * 100.0)
            if len(returns) < 20:
                return None

            current_ret = returns[-1]
            hist_ret = returns[:-1]
            price_z = self._compute_z_score(current_ret, hist_ret)

            current_vol = volumes[-1]
            hist_vol = volumes[:-1]
            volume_z = self._compute_z_score(current_vol, hist_vol)
            avg_vol = float(np.mean(np.array(hist_vol))) if hist_vol else 0.0
            volume_ratio = (current_vol / avg_vol) if avg_vol > 0 else 1.0

            news_velocity, news_sentiment = await self._news_metrics(session, instrument.symbol)
            score = self._compute_anomaly_score(price_z, volume_z, news_velocity)
            direction = "up" if current_ret >= 0 else "down"
            return AnomalySignal(
                symbol=instrument.symbol,
                asset_class=instrument.asset_class,
                timestamp=datetime.now(timezone.utc).isoformat(),
                price_move_pct=current_ret,
                price_z_score=price_z,
                volume_ratio=volume_ratio,
                volume_z_score=volume_z,
                news_velocity=news_velocity,
                news_sentiment=news_sentiment,
                anomaly_score=score,
                direction=direction,
                sector=instrument.sector,
                region=instrument.region,
            )

    async def _news_metrics(self, session, symbol: str) -> tuple[float, float]:
        now = datetime.now(timezone.utc)
        one_hour = now - timedelta(hours=1)
        day_ago = now - timedelta(hours=24)

        news_q = await session.execute(
            select(NewsHeadline)
            .where(NewsHeadline.published_at >= day_ago)
            .order_by(desc(NewsHeadline.published_at))
            .limit(500)
        )
        news_rows = list(news_q.scalars().all())
        sym = symbol.strip().upper()
        last_hour = 0
        total_day = 0
        for n in news_rows:
            title = (n.title or "").upper()
            desc_txt = (n.description or "").upper()
            if sym in title or sym in desc_txt:
                total_day += 1
                if n.published_at >= one_hour:
                    last_hour += 1
        baseline = max(1.0, total_day / 24.0)
        velocity = float(last_hour) / baseline

        ai_q = await session.execute(
            select(AIOutputLog)
            .where(AIOutputLog.symbol == symbol)
            .where(AIOutputLog.context_type == "news")
            .order_by(desc(AIOutputLog.timestamp))
            .limit(30)
        )
        ai_rows = list(ai_q.scalars().all())
        if not ai_rows:
            return velocity, 0.0
        vals = [float(r.score) for r in ai_rows if r.score is not None]
        if not vals:
            return velocity, 0.0
        return velocity, float(np.mean(np.array(vals)))

    def _compute_z_score(self, current_value: float, history: list[float]) -> float:
        if len(history) < 20:
            return 0.0
        arr = np.array(history)
        mean = float(np.mean(arr))
        std = float(np.std(arr))
        if std == 0:
            return 0.0
        return (current_value - mean) / std

    def _compute_anomaly_score(self, price_z: float, volume_z: float, news_velocity: float) -> float:
        price_component = min(abs(price_z) / 4.0, 1.0)
        volume_component = min(abs(volume_z) / 4.0, 1.0)
        news_component = min(news_velocity / 5.0, 1.0)
        score = (price_component * 0.50) + (volume_component * 0.30) + (news_component * 0.20)
        return round(score, 4)
