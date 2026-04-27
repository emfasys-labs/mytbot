"""
models/options/iv_surface.py
==============================
Wave 12 — implied-volatility surface.

Stores ``(strike, time_to_expiry_years) → iv`` points and provides
bilinear interpolation. Cheap arbitrage sanity checks are applied at
build time:

  - All IVs must be positive and finite.
  - Calendar arbitrage screen: for fixed strike, IV should not
    decrease drastically across longer expiries (heuristic threshold).
  - Strike monotonicity is NOT enforced — smile / skew is real.

The surface deliberately does NOT solve for IV from market premiums —
that's a calibration step left to the operator's data pipeline. This
module is the lookup / interpolation surface only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass(frozen=True)
class IVPoint:
    strike: float
    time_to_expiry_years: float
    iv: float


@dataclass
class IVSurface:
    """Bilinear-interpolated IV surface."""

    points: list[IVPoint] = field(default_factory=list)
    notes: str = ""

    # ── lookup ─────────────────────────────────────────────────────────────

    def lookup(self, *, strike: float, time_to_expiry_years: float) -> Optional[float]:
        """
        Bilinear interpolation. Returns ``None`` when the surface is
        empty. For (strike, T) outside the convex hull of the grid, the
        nearest in-grid point is used (clamped lookup).
        """
        if not self.points:
            return None
        # Build sorted unique axes.
        strikes = sorted({p.strike for p in self.points})
        ttes = sorted({p.time_to_expiry_years for p in self.points})

        k = float(strike)
        t = float(time_to_expiry_years)
        k = max(strikes[0], min(strikes[-1], k))
        t = max(ttes[0], min(ttes[-1], t))

        # Find bracketing indices on each axis.
        def _bracket(axis: list[float], v: float) -> tuple[int, int]:
            for i in range(len(axis) - 1):
                if axis[i] <= v <= axis[i + 1]:
                    return i, i + 1
            return len(axis) - 1, len(axis) - 1

        i0, i1 = _bracket(strikes, k)
        j0, j1 = _bracket(ttes, t)
        k0, k1 = strikes[i0], strikes[i1]
        t0, t1 = ttes[j0], ttes[j1]

        def _iv_at(kk: float, tt: float) -> Optional[float]:
            for p in self.points:
                if abs(p.strike - kk) < 1e-12 and abs(p.time_to_expiry_years - tt) < 1e-12:
                    return p.iv
            return None

        v00 = _iv_at(k0, t0)
        v01 = _iv_at(k0, t1)
        v10 = _iv_at(k1, t0)
        v11 = _iv_at(k1, t1)
        # Fall back to nearest available corner if any cell is missing.
        corners = [v for v in (v00, v01, v10, v11) if v is not None]
        if not corners:
            return None
        if any(v is None for v in (v00, v01, v10, v11)):
            return float(sum(corners) / len(corners))

        if k1 == k0 and t1 == t0:
            return float(v00)
        if k1 == k0:
            wt = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return float(v00 * (1 - wt) + v01 * wt)
        if t1 == t0:
            wk = (k - k0) / (k1 - k0)
            return float(v00 * (1 - wk) + v10 * wk)

        wk = (k - k0) / (k1 - k0)
        wt = (t - t0) / (t1 - t0)
        v_t0 = v00 * (1 - wk) + v10 * wk
        v_t1 = v01 * (1 - wk) + v11 * wk
        return float(v_t0 * (1 - wt) + v_t1 * wt)


# ── builder ────────────────────────────────────────────────────────────────


def build_iv_surface(
    points: Iterable[IVPoint],
    *,
    calendar_decrease_tolerance: float = 0.5,
) -> IVSurface:
    """
    Validate + assemble an ``IVSurface``.

    Rejects points with non-positive / non-finite IV. Runs a cheap
    calendar-arbitrage screen: for fixed strike, sorting expiries
    ascending, IV may not drop by more than ``calendar_decrease_tolerance``
    multiplicatively between adjacent expiries. The screen is a sanity
    check — operator should run a proper no-arbitrage smoothing
    upstream when assembling the data.
    """
    pts: list[IVPoint] = []
    for p in points:
        if not math.isfinite(p.iv) or p.iv <= 0:
            continue
        if not math.isfinite(p.strike) or p.strike <= 0:
            continue
        if not math.isfinite(p.time_to_expiry_years) or p.time_to_expiry_years <= 0:
            continue
        pts.append(p)
    if not pts:
        return IVSurface(points=[], notes="empty_after_validation")

    # Calendar screen.
    by_strike: dict[float, list[IVPoint]] = {}
    for p in pts:
        by_strike.setdefault(p.strike, []).append(p)
    notes_parts: list[str] = []
    for k, group in by_strike.items():
        ordered = sorted(group, key=lambda p: p.time_to_expiry_years)
        for a, b in zip(ordered[:-1], ordered[1:]):
            if a.iv <= 0:
                continue
            ratio = b.iv / a.iv
            if ratio < calendar_decrease_tolerance:
                notes_parts.append(
                    f"calendar_arbitrage_at_strike={k} ratio={ratio:.3f}"
                )

    notes = "; ".join(notes_parts) if notes_parts else ""
    return IVSurface(points=pts, notes=notes)
