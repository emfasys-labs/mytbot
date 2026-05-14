"""
system/adaptive_cadence.py
===========================
Compute the trading loop's next-iteration sleep dynamically from market
state. Replaces the static per-mode ``loop_cadence_sec`` map (hunter=120,
trader=300, defender=900) with a function of objective signals:

  * **signal density** — candidates generated last iteration. Rising
    density means the strategies are seeing opportunities now, so we tick
    faster to catch them.
  * **session window** — regular US market hours get a faster baseline;
    overnight / weekend slow down (less likely to find tradable edge in
    a thin tape).
  * **mode** — the adaptive_mode classifier output biases the multiplier:
    hunter pulls the cadence toward the fast end of the band; defender
    pulls it toward the slow end. Mode is itself derived from market
    state, so this isn't a static knob — it's a downstream consequence.

The function is **pure** for testability and falls back to the static
``base_interval`` on any error. Floor/ceiling are configurable via env
vars (`ADAPTIVE_CADENCE_FLOOR_SEC`, `ADAPTIVE_CADENCE_CEILING_SEC`).

Design intent for the user's "as much money as possible" north star:
when signals are flowing **we don't want to be sleeping**. The previous
2-minute floor was an arbitrary heuristic; this lets the loop pace itself
to the market's actual signal generation rate.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Optional


@dataclass(frozen=True)
class CadenceInputs:
    """Snapshot for the cadence calculator. Same philosophy as
    ``ModeInputs``: missing values fall through to safe defaults."""

    # Mode label from the adaptive_mode classifier (hunter/trader/defender).
    mode: str = "hunter"
    # Candidates generated last iteration (None on fresh boot).
    recent_signal_density: Optional[float] = None
    # Loop's static fallback interval (e.g. self.loop_interval_sec).
    base_interval_sec: float = 120.0
    # Override for tests.
    now: Optional[datetime] = None


_FLOOR_SEC = float(os.getenv("ADAPTIVE_CADENCE_FLOOR_SEC", "30"))
_CEILING_SEC = float(os.getenv("ADAPTIVE_CADENCE_CEILING_SEC", "600"))

# Signal-density bands. "Burst" = strategies are clearly finding setups;
# "normal" = steady flow; "dry" = nothing's coming through. Cadence
# accelerates in burst, decelerates in dry.
_DENSITY_BURST_THRESHOLD = float(os.getenv("ADAPTIVE_CADENCE_BURST_THRESHOLD", "10.0"))
_DENSITY_DRY_THRESHOLD = float(os.getenv("ADAPTIVE_CADENCE_DRY_THRESHOLD", "1.0"))


def _session_baseline_multiplier(now: datetime) -> float:
    """Faster in regular US session, slower overnight / weekend.

    The mid-Atlantic session (US 13:30-20:00 UTC) is when the bulk of
    the universe trades; weekends and Asian session see thin tapes and
    cluttered signals that don't translate to fillable orders. The
    multiplier scales the base interval — 1.0 = normal pace, 1.5 = 50%
    slower than normal.
    """
    if now.weekday() >= 5:
        return 1.5  # weekend — crypto-only liquid
    t = now.timetz().replace(tzinfo=None)
    if time(13, 30) <= t <= time(20, 0):
        return 1.0  # regular US session — full pace
    return 1.25  # overnight — slow down a bit


def _mode_multiplier(mode: str) -> float:
    """Mode bias on cadence. Hunter is the fastest, defender the slowest.

    Note these multipliers are applied AFTER the dynamic factors so the
    market-driven signals dominate; mode is just a tie-breaker / bias.
    """
    m = (mode or "hunter").strip().lower()
    if m == "defender":
        return 2.5  # 2.5x slower — let setups mature, conserve effort
    if m == "trader":
        return 1.4  # 40% slower than hunter
    return 1.0  # hunter default — fastest


def _density_multiplier(density: Optional[float]) -> float:
    """Translate signal density into a cadence multiplier.

    * burst (>=10 signals last tick) → 0.4x — speed up dramatically
    * normal (1-10) → 1.0x
    * dry (<1) → 2.0x — slow down, wait for setups to develop

    None (fresh boot) → 1.0x.
    """
    if density is None:
        return 1.0
    if density >= _DENSITY_BURST_THRESHOLD:
        return 0.4
    if density < _DENSITY_DRY_THRESHOLD:
        return 2.0
    return 1.0


def compute_loop_cadence(inputs: CadenceInputs) -> float:
    """Return the next sleep interval in seconds.

    Formula: ``base_interval × session × mode × density``,
    clamped to ``[FLOOR, CEILING]``.

    The base interval is the operator's configured "default" pace. The
    three multipliers are independent dimensions — session captures *when*,
    mode captures *how aggressive*, density captures *how busy*.

    Example flow with base=120:
      * Hunter, regular session, normal density: 120 × 1.0 × 1.0 × 1.0 = 120s
      * Hunter, regular session, BURST: 120 × 1.0 × 1.0 × 0.4 = 48s
      * Defender, overnight, dry: 120 × 1.25 × 2.5 × 2.0 = 750s → clamped to 600s
    """
    now = inputs.now or datetime.now(timezone.utc)
    base = max(1.0, float(inputs.base_interval_sec))
    sess = _session_baseline_multiplier(now)
    mode = _mode_multiplier(inputs.mode)
    dens = _density_multiplier(inputs.recent_signal_density)
    raw = base * sess * mode * dens
    return max(_FLOOR_SEC, min(_CEILING_SEC, raw))
