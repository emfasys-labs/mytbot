"""Cross-section inputs for D015 regime state (M2 feature_snapshots + ai_outputs)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from storage.models import AIOutputLog, FeatureSnapshot


async def fetch_latest_feature_rows(
    session: AsyncSession,
    symbols: list[str],
    timeframe: str,
) -> list[dict[str, Any]]:
    """Latest bar per symbol — portable across Postgres and SQLite.

    Uses a ``ROW_NUMBER()`` window (replacing PostgreSQL ``DISTINCT ON`` +
    ``ANY(array)``) so the Lite/SQLite profile works identically.
    """
    if not symbols:
        return []
    tf = timeframe[:8]
    syms = [s[:32] for s in symbols]
    rn = (
        func.row_number()
        .over(
            partition_by=FeatureSnapshot.symbol,
            order_by=FeatureSnapshot.bar_timestamp.desc(),
        )
        .label("rn")
    )
    ranked = (
        select(
            FeatureSnapshot.symbol.label("symbol"),
            FeatureSnapshot.features.label("features"),
            FeatureSnapshot.bar_timestamp.label("bar_timestamp"),
            rn,
        )
        .where(FeatureSnapshot.timeframe == tf, FeatureSnapshot.symbol.in_(syms))
        .subquery()
    )
    stmt = (
        select(ranked.c.symbol, ranked.c.features, ranked.c.bar_timestamp)
        .where(ranked.c.rn == 1)
    )
    rows = (await session.execute(stmt)).fetchall()
    return [{"symbol": str(r[0]), "features": dict(r[1] or {}), "bar_timestamp": r[2]} for r in rows]


async def fetch_news_score_dispersion(
    session: AsyncSession,
    *,
    lookback_hours: int,
) -> tuple[float, float] | None:
    """
    Return (mean, std) of recent news AI scores, or None if insufficient rows.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, lookback_hours))
    stmt = (
        select(AIOutputLog.score)
        .where(AIOutputLog.context_type == "news")
        .where(AIOutputLog.timestamp >= since)
        .where(AIOutputLog.score.isnot(None))
        .limit(500)
    )
    vals = [float(r[0]) for r in (await session.execute(stmt)).fetchall() if r[0] is not None]
    if len(vals) < 3:
        return None
    m = sum(vals) / len(vals)
    var = sum((x - m) ** 2 for x in vals) / len(vals)
    return m, math.sqrt(var)


def cross_section_from_feature_rows(
    rows: list[dict[str, Any]],
    *,
    anomaly_volume_z_threshold: float,
    anomaly_rel_dv_threshold: float,
    broker_liquidity_score: float | None = None,
    broker_weight: float = 0.0,
    feature_weight: float = 1.0,
) -> dict[str, float]:
    """
    Deterministic floats in [0,1] or signed where noted. Safe with empty rows.
    """
    if not rows:
        return {
            "trend_strength": 0.0,
            "cross_asset_confirmation": 0.0,
            "liquidity_state": 0.0,
            "macro_clarity": 0.0,
            "risk_on_breadth": 0.0,
            "chaos_penalty": 0.0,
            "correlation_crowding": 0.0,
            "volatility_structure": 0.0,
            "anomaly_breadth": 0.0,
            "news_conflict_score": 0.0,
            "symbol_count": 0.0,
        }

    moms: list[float] = []
    rsis: list[float] = []
    volzs: list[float] = []
    rel_dvs: list[float] = []
    atr_pcts: list[float] = []
    hursts: list[float] = []
    anom_flags: list[float] = []

    for r in rows:
        f = r.get("features") or {}
        mom = f.get("mom_10")
        rsi = f.get("rsi_14")
        vz = f.get("volume_z")
        rdv = f.get("relative_dollar_volume")
        atr = f.get("atr_14")
        hurst = f.get("hurst_dfa_128")
        gv = f.get("garch_vol_1d")
        if mom is not None and not (isinstance(mom, float) and math.isnan(mom)):
            moms.append(float(mom))
        if rsi is not None and not (isinstance(rsi, float) and math.isnan(rsi)):
            rsis.append(float(rsi))
        if vz is not None and not (isinstance(vz, float) and math.isnan(vz)):
            volzs.append(float(vz))
        if rdv is not None and not (isinstance(rdv, float) and math.isnan(rdv)):
            rel_dvs.append(float(rdv))
        if gv is not None and not (isinstance(gv, float) and math.isnan(gv)):
            atr_pcts.append(float(gv))
        elif atr is not None and not (isinstance(atr, float) and math.isnan(atr)):
            atr_pcts.append(float(atr) / 100.0)
        if hurst is not None and not (isinstance(hurst, float) and math.isnan(hurst)):
            hursts.append(float(hurst))
        vz_f = float(vz) if vz is not None and not (isinstance(vz, float) and math.isnan(vz)) else 0.0
        rdv_f = float(rdv) if rdv is not None and not (isinstance(rdv, float) and math.isnan(rdv)) else 1.0
        flag = 1.0 if (vz_f > anomaly_volume_z_threshold or rdv_f > 1.0 + anomaly_rel_dv_threshold) else 0.0
        anom_flags.append(flag)

    def _mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    def _sign_agree(xs: list[float]) -> float:
        if len(xs) < 2:
            return 0.0
        pos = sum(1 for x in xs if x > 0)
        neg = sum(1 for x in xs if x < 0)
        return max(pos, neg) / len(xs)

    trend_strength = _sign_agree(moms) if moms else 0.0
    if rsis:
        rsi_breadth = sum(1 for x in rsis if x > 55) / len(rsis)
        trend_strength = max(trend_strength, rsi_breadth)

    cross_asset_confirmation = trend_strength * 0.85 + (1.0 - min(1.0, _mean([abs(x) for x in moms]) / 50.0)) * 0.15 if moms else trend_strength

    vol_structure = min(1.0, _mean(atr_pcts) / 0.05) if atr_pcts else min(1.0, abs(_mean(volzs)) / 3.0 if volzs else 0.0)

    chaos = min(1.0, _mean([abs(x) for x in volzs]) / 3.0) if volzs else 0.0
    if hursts:
        chaos = max(chaos, min(1.0, abs(0.5 - _mean(hursts)) * 2.0))

    anomaly_breadth = _mean(anom_flags) if anom_flags else 0.0

    # Liquidity proxy: lower score when vol-of-vol high; optional broker depth blend
    liq_feature = max(0.0, 1.0 - chaos * 0.9)
    liq = liq_feature
    s_bw = broker_weight + feature_weight
    if broker_liquidity_score is not None and s_bw > 0:
        liq = (broker_weight * broker_liquidity_score + feature_weight * liq_feature) / s_bw

    # Correlation crowding: mom direction agreement
    crowding = _sign_agree(moms) if len(moms) >= 3 else 0.0

    return {
        "trend_strength": float(min(1.0, max(0.0, trend_strength))),
        "cross_asset_confirmation": float(min(1.0, max(0.0, cross_asset_confirmation))),
        "liquidity_state": float(min(1.0, max(0.0, liq))),
        "macro_clarity": 0.0,
        "risk_on_breadth": float(min(1.0, max(0.0, _mean([1.0 if x > 0 else 0.0 for x in moms])))) if moms else 0.0,
        "chaos_penalty": float(min(1.0, max(0.0, chaos))),
        "correlation_crowding": float(min(1.0, max(0.0, crowding))),
        "volatility_structure": float(min(1.0, max(0.0, vol_structure))),
        "anomaly_breadth": float(min(1.0, max(0.0, anomaly_breadth))),
        "news_conflict_score": 0.0,
        "symbol_count": float(len(rows)),
    }
