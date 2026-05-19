"""D117 — Adaptive universe-tier sizing policy.

This module is a *pure decision module*. It takes a snapshot of recent
market conditions (regime label, signal pressure, active cluster count)
plus a static config bound, and returns the resolved universe-tier caps
the pipeline / UniverseBuilder should use for the next refresh cycle.

Design contract:

- **No I/O**. Loading the YAML and the runtime context lives elsewhere
  (see :mod:`universe.adaptive_context` and the orchestrator wiring).
- **Bounded**. Resolved caps are always clamped to YAML-declared
  ``min/max`` ranges so a misbehaving regime/signal-pressure spike
  cannot inflate caps unboundedly.
- **Deterministic**. Same inputs → same outputs; safe to unit test.
- **Backward-compatible**. When ``adaptive.enabled`` is false (or the
  YAML is missing/invalid) we return the base caps untouched and the
  trading pipeline behaves exactly as before D117.
- **Auditable**. Every resolved cap carries a list of human-readable
  ``reasons`` explaining why it ended up at that value, plus the
  composite multiplier so the dashboard can render the decision.

We deliberately do NOT touch ``risk/engine.py`` or any order/risk path:
adaptive caps are a *discovery layer* control. Risk vetoes are
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdaptiveCapsBase:
    """The static caps coming from ``config/data_pipeline.yaml``.

    These are the pre-D117 numbers the funnel/builder used unconditionally.
    They act as the *neutral anchor* for the adaptive multiplier.
    """

    candidates: int
    watching: int
    core: int
    scan: int

    def as_dict(self) -> dict[str, int]:
        return {
            "candidates": int(self.candidates),
            "watching": int(self.watching),
            "core": int(self.core),
            "scan": int(self.scan),
        }


@dataclass(frozen=True)
class AdaptiveCapsContext:
    """The runtime context the policy uses to grow/shrink caps."""

    regime_label: str = "mixed"
    breadth_score: float | None = None
    signal_pressure: int | None = None
    active_cluster_count: int | None = None
    note: str | None = None


# ---------------------------------------------------------------------------
# Config schema (loaded from data_pipeline.yaml::dynamic_universe.adaptive)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AxisMultiplier:
    risk_on: float = 1.25
    risk_off: float = 0.80
    volatile: float = 1.30
    mixed: float = 1.00
    crash: float = 0.65
    trend_up: float = 1.15
    trend_down: float = 1.00
    range: float = 0.90
    insufficient_data: float = 1.00
    unknown: float = 1.00


@dataclass(frozen=True)
class _SignalPressurePolicy:
    high_threshold: int = 8
    low_threshold: int = 2
    high_multiplier: float = 1.20
    low_multiplier: float = 0.80


@dataclass(frozen=True)
class _ClusterAwarePolicy:
    enabled: bool = True
    watching_min_factor: float = 3.0
    watching_min_floor: int = 150


@dataclass(frozen=True)
class _ChurnPolicy:
    min_consecutive_drops: int = 3


@dataclass(frozen=True)
class _CapBounds:
    min: int
    max: int

    def clamp(self, value: int) -> int:
        return max(int(self.min), min(int(self.max), int(value)))


@dataclass(frozen=True)
class AdaptiveCapsConfig:
    """Resolved adaptive-caps configuration with safe defaults."""

    enabled: bool = False
    bounds: Mapping[str, _CapBounds] = field(default_factory=dict)
    regime: _AxisMultiplier = _AxisMultiplier()
    signal_pressure: _SignalPressurePolicy = _SignalPressurePolicy()
    cluster_aware: _ClusterAwarePolicy = _ClusterAwarePolicy()
    churn: _ChurnPolicy = _ChurnPolicy()


def _safe_int(x: Any, fallback: int) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return int(fallback)


def _safe_float(x: Any, fallback: float) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float(fallback)


def _parse_axis(mapping: Any, defaults: _AxisMultiplier) -> _AxisMultiplier:
    if not isinstance(mapping, Mapping):
        return defaults

    def _multiplier(key: str, default: float) -> float:
        entry = mapping.get(key)
        if isinstance(entry, Mapping):
            return _safe_float(entry.get("multiplier"), default)
        return _safe_float(entry, default)

    return _AxisMultiplier(
        risk_on=_multiplier("risk_on", defaults.risk_on),
        risk_off=_multiplier("risk_off", defaults.risk_off),
        volatile=_multiplier("volatile", defaults.volatile),
        mixed=_multiplier("mixed", defaults.mixed),
        crash=_multiplier("crash", defaults.crash),
        trend_up=_multiplier("trend_up", defaults.trend_up),
        trend_down=_multiplier("trend_down", defaults.trend_down),
        range=_multiplier("range", defaults.range),
        insufficient_data=_multiplier("insufficient_data", defaults.insufficient_data),
        unknown=_multiplier("unknown", defaults.unknown),
    )


def parse_adaptive_caps_config(blob: Any) -> AdaptiveCapsConfig:
    """Parse the ``dynamic_universe.adaptive`` YAML section into a dataclass.

    The parser is intentionally lenient: any missing/invalid key falls
    back to the documented default. Returns a config with ``enabled=False``
    when the input is not a mapping.
    """
    if not isinstance(blob, Mapping):
        return AdaptiveCapsConfig()

    bounds_blob = blob.get("bounds") if isinstance(blob.get("bounds"), Mapping) else {}
    bounds: dict[str, _CapBounds] = {}
    defaults_bounds = {
        "candidates": _CapBounds(min=200, max=800),
        "watching": _CapBounds(min=150, max=600),
        "core": _CapBounds(min=25, max=100),
        "scan": _CapBounds(min=75, max=500),
    }
    for tier, default in defaults_bounds.items():
        entry = bounds_blob.get(tier) if isinstance(bounds_blob.get(tier), Mapping) else None
        if entry:
            bounds[tier] = _CapBounds(
                min=_safe_int(entry.get("min"), default.min),
                max=_safe_int(entry.get("max"), default.max),
            )
        else:
            bounds[tier] = default

    signal_blob = blob.get("signal_pressure") if isinstance(blob.get("signal_pressure"), Mapping) else {}
    cluster_blob = blob.get("cluster_aware") if isinstance(blob.get("cluster_aware"), Mapping) else {}
    churn_blob = blob.get("churn") if isinstance(blob.get("churn"), Mapping) else {}

    return AdaptiveCapsConfig(
        enabled=bool(blob.get("enabled", False)),
        bounds=bounds,
        regime=_parse_axis(blob.get("regime"), _AxisMultiplier()),
        signal_pressure=_SignalPressurePolicy(
            high_threshold=_safe_int(signal_blob.get("high_threshold"), 8),
            low_threshold=_safe_int(signal_blob.get("low_threshold"), 2),
            high_multiplier=_safe_float(signal_blob.get("high_multiplier"), 1.20),
            low_multiplier=_safe_float(signal_blob.get("low_multiplier"), 0.80),
        ),
        cluster_aware=_ClusterAwarePolicy(
            enabled=bool(cluster_blob.get("enabled", True)),
            watching_min_factor=_safe_float(cluster_blob.get("watching_min_factor"), 3.0),
            watching_min_floor=_safe_int(cluster_blob.get("watching_min_floor"), 150),
        ),
        churn=_ChurnPolicy(
            min_consecutive_drops=max(1, _safe_int(churn_blob.get("min_consecutive_drops"), 3)),
        ),
    )


def load_adaptive_caps_config(path: Path | None = None) -> AdaptiveCapsConfig:
    """Load adaptive caps from ``config/data_pipeline.yaml`` (or override path).

    Any YAML / IO error returns a disabled config so the pipeline keeps
    using static caps.
    """
    p = path or Path("config/data_pipeline.yaml")
    if not p.is_file():
        return AdaptiveCapsConfig()
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return AdaptiveCapsConfig()
    if not isinstance(raw, Mapping):
        return AdaptiveCapsConfig()
    section = raw.get("dynamic_universe") or {}
    if not isinstance(section, Mapping):
        return AdaptiveCapsConfig()
    return parse_adaptive_caps_config(section.get("adaptive"))


# ---------------------------------------------------------------------------
# Result + computation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdaptiveCapsResult:
    """The resolved caps, plus the reasons / multipliers that produced them."""

    candidates: int
    watching: int
    core: int
    scan: int
    base: AdaptiveCapsBase
    multiplier: float
    regime_multiplier: float
    signal_pressure_multiplier: float
    cluster_floor_applied: bool
    reasons: list[str]
    enabled: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates": int(self.candidates),
            "watching": int(self.watching),
            "core": int(self.core),
            "scan": int(self.scan),
            "base": self.base.as_dict(),
            "multiplier": round(float(self.multiplier), 4),
            "regime_multiplier": round(float(self.regime_multiplier), 4),
            "signal_pressure_multiplier": round(float(self.signal_pressure_multiplier), 4),
            "cluster_floor_applied": bool(self.cluster_floor_applied),
            "reasons": list(self.reasons),
            "enabled": bool(self.enabled),
        }


def _regime_multiplier(policy: _AxisMultiplier, label: str | None) -> tuple[float, str]:
    key = (label or "").strip().lower() or "unknown"
    table: dict[str, float] = {
        "risk_on": policy.risk_on,
        "risk_off": policy.risk_off,
        "volatile": policy.volatile,
        "mixed": policy.mixed,
        "crash": policy.crash,
        "trend_up": policy.trend_up,
        "trend_down": policy.trend_down,
        "range": policy.range,
        "insufficient_data": policy.insufficient_data,
    }
    if key not in table:
        return policy.unknown, "unknown"
    return float(table[key]), key


def _signal_pressure_multiplier(
    policy: _SignalPressurePolicy, value: int | None
) -> tuple[float, str]:
    if value is None or int(value) < 0:
        return 1.0, "unknown"
    v = int(value)
    if v >= int(policy.high_threshold):
        return float(policy.high_multiplier), f"high(>={policy.high_threshold})"
    if v <= int(policy.low_threshold):
        return float(policy.low_multiplier), f"low(<={policy.low_threshold})"
    return 1.0, "neutral"


def compute_adaptive_caps(
    *,
    base: AdaptiveCapsBase,
    context: AdaptiveCapsContext,
    config: AdaptiveCapsConfig,
) -> AdaptiveCapsResult:
    """Compute the resolved caps for the next pipeline tick.

    When ``config.enabled`` is false (or the config is otherwise neutral)
    we return ``base`` unchanged with a single ``reason='adaptive_disabled'``.
    """
    if not config.enabled:
        return AdaptiveCapsResult(
            candidates=int(base.candidates),
            watching=int(base.watching),
            core=int(base.core),
            scan=int(base.scan),
            base=base,
            multiplier=1.0,
            regime_multiplier=1.0,
            signal_pressure_multiplier=1.0,
            cluster_floor_applied=False,
            reasons=["adaptive_disabled"],
            enabled=False,
        )

    reasons: list[str] = []

    regime_mult, regime_key = _regime_multiplier(config.regime, context.regime_label)
    reasons.append(f"regime={regime_key}:{regime_mult:.2f}")

    sp_mult, sp_key = _signal_pressure_multiplier(config.signal_pressure, context.signal_pressure)
    if context.signal_pressure is None:
        reasons.append("signal_pressure=unknown:1.00")
    else:
        reasons.append(f"signal_pressure={sp_key}:{sp_mult:.2f}")

    composite = float(regime_mult) * float(sp_mult)

    def _resolve(tier: str, base_value: int) -> int:
        target = int(round(base_value * composite))
        bounds = config.bounds.get(tier)
        if bounds is not None:
            return bounds.clamp(target)
        # No bounds for this tier — fall back to a safe symmetric clamp.
        return max(1, target)

    candidates = _resolve("candidates", base.candidates)
    watching = _resolve("watching", base.watching)
    core = _resolve("core", base.core)
    scan = _resolve("scan", base.scan)

    # Cluster-aware floor: when we have an honest live cluster count,
    # ensure ``watching`` covers at least ``watching_min_factor * clusters``
    # (subject to the watching bounds and the absolute floor).
    cluster_floor_applied = False
    if (
        config.cluster_aware.enabled
        and context.active_cluster_count is not None
        and int(context.active_cluster_count) > 0
    ):
        wanted = int(
            max(
                config.cluster_aware.watching_min_floor,
                round(config.cluster_aware.watching_min_factor * int(context.active_cluster_count)),
            )
        )
        bounds = config.bounds.get("watching")
        if bounds is not None:
            wanted = bounds.clamp(wanted)
        if wanted > watching:
            reasons.append(
                f"cluster_floor=({context.active_cluster_count}*{config.cluster_aware.watching_min_factor:.1f})->{wanted}"
            )
            watching = wanted
            cluster_floor_applied = True

    # ``core`` should not exceed ``watching`` (core is a strict subset).
    core = min(core, watching)
    # ``scan`` should not exceed ``candidates - core``; the scan tier is
    # everything that fits between the core and the candidate ceiling.
    scan = min(scan, max(0, candidates - core))

    return AdaptiveCapsResult(
        candidates=int(candidates),
        watching=int(watching),
        core=int(core),
        scan=int(scan),
        base=base,
        multiplier=composite,
        regime_multiplier=float(regime_mult),
        signal_pressure_multiplier=float(sp_mult),
        cluster_floor_applied=cluster_floor_applied,
        reasons=reasons,
        enabled=True,
    )


# ---------------------------------------------------------------------------
# Anti-churn hysteresis (watchlist drops)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChurnHysteresisResult:
    """Final tier sets after hysteresis is applied + the updated miss counter."""

    core: tuple[str, ...]
    scan: tuple[str, ...]
    light: tuple[str, ...]
    consecutive_misses: dict[str, int]
    grace_extended: tuple[str, ...]


def _normalise_symbols(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values or []:
        s = str(v).strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def apply_churn_hysteresis(
    *,
    new_core: Iterable[str],
    new_scan: Iterable[str],
    new_light: Iterable[str],
    previous_core: Iterable[str] = (),
    previous_scan: Iterable[str] = (),
    consecutive_misses: Mapping[str, int] | None = None,
    policy: _ChurnPolicy | None = None,
) -> ChurnHysteresisResult:
    """Carry watchlist symbols across rebuilds until they fail N times in a row.

    A symbol that was in last build's ``core`` or ``scan`` but is missing
    from this build's combined set keeps a *grace* slot in ``scan`` for
    up to ``min_consecutive_drops`` consecutive rebuilds. After that it
    drops to ``light`` (still scored, just not in the active watchlist).

    Returns the (possibly extended) ``core``, ``scan``, ``light`` plus
    the updated consecutive-miss counter — the orchestrator persists
    that counter so subsequent rebuilds know how many times each grace
    slot has already been used.
    """
    pol = policy or _ChurnPolicy()
    nc = _normalise_symbols(new_core)
    ns = _normalise_symbols(new_scan)
    nl = _normalise_symbols(new_light)
    pc = set(_normalise_symbols(previous_core))
    ps = set(_normalise_symbols(previous_scan))

    new_watch = set(nc) | set(ns)
    previous_watch = pc | ps
    misses = {k.upper(): int(v) for k, v in (consecutive_misses or {}).items() if int(v) >= 0}

    # Anything that came back into the watchlist resets its miss counter.
    for sym in new_watch:
        if sym in misses:
            del misses[sym]

    # Find missing-from-watchlist symbols that *were* there before and
    # decide whether to extend their grace.
    grace_extended: list[str] = []
    extended_scan: list[str] = list(ns)
    for sym in sorted(previous_watch - new_watch):
        misses[sym] = misses.get(sym, 0) + 1
        if misses[sym] < int(pol.min_consecutive_drops):
            extended_scan.append(sym)
            grace_extended.append(sym)

    # Re-dedupe and keep light untouched; anything dropped from core/scan
    # that is being graced lands in scan as a temporary holder. Light is
    # whatever was already light minus anything we just re-promoted.
    extended_scan_set = set(extended_scan)
    final_scan = list(dict.fromkeys(extended_scan))
    final_light = [s for s in nl if s not in extended_scan_set and s not in set(nc)]

    # Symbols that have exceeded the grace budget are removed from misses
    # so the counter doesn't grow unboundedly on permanently-delisted names.
    for sym, count in list(misses.items()):
        if count >= int(pol.min_consecutive_drops):
            del misses[sym]

    return ChurnHysteresisResult(
        core=tuple(nc),
        scan=tuple(final_scan),
        light=tuple(final_light),
        consecutive_misses=misses,
        grace_extended=tuple(grace_extended),
    )
