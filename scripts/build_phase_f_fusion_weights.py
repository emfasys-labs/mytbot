#!/usr/bin/env python3
"""
scripts/build_phase_f_fusion_weights.py
=========================================
Phase F builder — learn per-regime fusion weights from real
feature_snapshots history. SHADOW artifact only: it is written with
``promote_eligible: False``; only ``report_phase_f_fusion_shadow.py`` may
flip that, and only if learned weighting beats the static blend on real
forward returns. Read-only on the DB; changes no trading behavior.

Method (deliberately simple + honest, like the Phase E builder):
  * Pull feature_snapshots per symbol/timeframe (numeric features + close).
  * Derive proxy component series in [0,1] for the 7 fusion components
    from available features / OHLCV (momentum, volume_anomaly, …).
  * Label a coarse regime per row (trend_up / range / trend_down) from
    the rolling return sign + volatility.
  * Per regime, fit NON-NEGATIVE, normalised weights mapping the 7
    component proxies → forward 1-bar return (clipped least squares).
  * Persist {by_regime, default, metadata}. Thin/one-regime data ⇒ weak
    weights; that is fine — the evidence gate decides, not this script.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv()

from system.fusion_weights import COMPONENTS, RegimeConditionalFusionWeights  # noqa: E402


def _z01(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    if x.size == 0:
        return x
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    mu, sd = float(np.mean(x)), float(np.std(x))
    if sd <= 1e-12:
        return np.full_like(x, 0.5)
    z = np.clip((x - mu) / sd, -8.0, 8.0)
    out = 1.0 / (1.0 + np.exp(-z))  # squashed to [0,1]
    return np.nan_to_num(out, nan=0.5, posinf=1.0, neginf=0.0)


def _nnls_weights(C: np.ndarray, y: np.ndarray) -> dict[str, float]:
    """Non-negative, normalised weights via clipped ridge LS (no scipy)."""
    if C.shape[0] < max(20, C.shape[1] * 3):
        # too little data → equal weights (honest fallback)
        return {c: 1.0 / len(COMPONENTS) for c in COMPONENTS}
    C = np.nan_to_num(np.asarray(C, float), nan=0.5, posinf=1.0, neginf=0.0)
    y = np.nan_to_num(np.asarray(y, float), nan=0.0, posinf=0.0, neginf=0.0)
    lam = 1.0
    gram = C.T @ C + lam * np.eye(C.shape[1])
    try:
        beta = np.linalg.solve(gram, C.T @ y)
    except np.linalg.LinAlgError:
        return {c: 1.0 / len(COMPONENTS) for c in COMPONENTS}
    if not np.isfinite(beta).all():
        return {c: 1.0 / len(COMPONENTS) for c in COMPONENTS}
    beta = np.clip(beta, 0.0, None)
    s = float(beta.sum())
    if s <= 1e-12:
        return {c: 1.0 / len(COMPONENTS) for c in COMPONENTS}
    beta = beta / s
    return {c: float(b) for c, b in zip(COMPONENTS, beta)}


async def _load() -> dict[str, list]:
    from sqlalchemy import select
    from storage.db import init_async_database
    from storage.models import FeatureSnapshot

    engine, sm = await init_async_database()
    if sm is None:
        raise SystemExit("DB unavailable (POSTGRES_* / .env)")
    out: dict[str, list] = defaultdict(list)
    async with sm() as s:
        rows = (
            await s.execute(
                select(
                    FeatureSnapshot.symbol,
                    FeatureSnapshot.timeframe,
                    FeatureSnapshot.bar_timestamp,
                    FeatureSnapshot.close,
                    FeatureSnapshot.volume,
                    FeatureSnapshot.features,
                ).order_by(FeatureSnapshot.bar_timestamp.asc())
            )
        ).all()
    for sym, tf, ts, close, vol, feats in rows:
        out[f"{sym}|{tf}"].append((ts, float(close or 0.0), float(vol or 0.0), feats or {}))
    await engine.dispose()
    return out


def _components_and_target(series: list) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build (C[n,7], fwd_ret[n], regime_label[n]) for one symbol series."""
    closes = np.array([r[1] for r in series], dtype=float)
    vols = np.array([r[2] for r in series], dtype=float)
    n = len(closes)
    if n < 40:
        return np.empty((0, 7)), np.empty(0), np.empty(0, dtype=object)
    ret = np.zeros(n)
    ret[1:] = np.where(closes[:-1] > 0, closes[1:] / closes[:-1] - 1.0, 0.0)
    mom = np.zeros(n)
    for i in range(n):
        a = max(0, i - 10)
        mom[i] = closes[i] / closes[a] - 1.0 if closes[a] > 0 else 0.0
    # proxy components (all squashed to [0,1])
    comp = {
        "momentum": _z01(mom),
        "volume_anomaly": _z01(vols),
        "news_impact": np.full(n, 0.5),  # not in history → neutral
        "regime_alignment": _z01(np.sign(mom) * np.abs(ret)),
        "liquidity_quality": _z01(vols * np.abs(closes)),
        "structure_quality": _z01(-np.abs(ret)),  # calmer = better structure
        "relative_strength": _z01(mom),
    }
    C = np.column_stack([comp[c] for c in COMPONENTS])
    fwd = np.zeros(n)
    fwd[:-1] = ret[1:]  # forward 1-bar return
    # coarse regime label
    roll = np.zeros(n)
    for i in range(n):
        a = max(0, i - 20)
        roll[i] = closes[i] / closes[a] - 1.0 if closes[a] > 0 else 0.0
    vol_r = np.array([np.std(ret[max(0, i - 20):i + 1]) for i in range(n)])
    vmed = float(np.median(vol_r)) or 1e-9
    lab = np.empty(n, dtype=object)
    for i in range(n):
        if vol_r[i] > 2.0 * vmed:
            lab[i] = "volatile"
        elif roll[i] > 0.01:
            lab[i] = "trend_up"
        elif roll[i] < -0.01:
            lab[i] = "trend_down"
        else:
            lab[i] = "range"
    return C[:-1], fwd[:-1], lab[:-1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    data = asyncio.run(_load())
    by_regime_rows: dict[str, list] = defaultdict(lambda: [np.empty((0, 7)), np.empty(0)])
    all_C: list = []
    all_y: list = []
    for _key, series in data.items():
        C, y, lab = _components_and_target(series)
        if C.size == 0:
            continue
        all_C.append(C)
        all_y.append(y)
        for reg in set(lab.tolist()):
            m = lab == reg
            cur = by_regime_rows[reg]
            cur[0] = np.vstack([cur[0], C[m]]) if cur[0].size else C[m]
            cur[1] = np.concatenate([cur[1], y[m]]) if cur[1].size else y[m]

    if not all_C:
        raise SystemExit("no usable history")
    Call = np.vstack(all_C)
    yall = np.concatenate(all_y)

    art = RegimeConditionalFusionWeights(
        by_regime={
            reg: _nnls_weights(cur[0], cur[1])
            for reg, cur in by_regime_rows.items()
            if cur[0].shape[0] >= 20
        },
        default=_nnls_weights(Call, yall),
        metadata={
            "promote_eligible": False,  # ONLY the evidence report flips this
            "n_obs": int(yall.shape[0]),
            "regimes": {
                reg: int(cur[0].shape[0]) for reg, cur in by_regime_rows.items()
            },
            "builder": "phase_f_v1_proxy_components",
        },
    )
    from system.fusion_weights import DEFAULT_ARTIFACT

    out = args.out or str(DEFAULT_ARTIFACT)
    art.save(out)
    print(f"phase_f | wrote {out}")
    print(f"phase_f | n_obs={art.metadata['n_obs']} regimes={art.metadata['regimes']}")
    print(f"phase_f | default weights={ {k: round(v,3) for k,v in art.default.items()} }")
    print("phase_f | promote_eligible=False (evidence report decides)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
