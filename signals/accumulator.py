"""
signals/accumulator.py
======================
Stateful, time-decayed per-symbol signal memory (quant + news + macro).

Feeds the SignalEngine with NetSignal scores/confidence; does not bypass risk.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from ai.pipeline import AIPipelineResult
from core.models_runtime import clip_decimal
from core.signal_math import tanh_clip

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0")
DECIMAL_ONE = Decimal("1")
DECIMAL_MINUS_ONE = Decimal("-1")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def d(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def sign_of(value: Decimal) -> int:
    if value > DECIMAL_ZERO:
        return 1
    if value < DECIMAL_ZERO:
        return -1
    return 0


@dataclass(slots=True)
class InputSignal:
    """
    Normalised signal entering the accumulation layer.

    direction: -1 bearish, 0 neutral, +1 bullish
    strength, confidence: [0, 1]
    horizon: short | medium | long
    source_type: quant | news | macro | cross_asset
    """

    symbol: str
    source_type: str
    source_name: str
    direction: int
    strength: Decimal
    confidence: Decimal
    horizon: str
    timestamp: datetime
    half_life_minutes: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.symbol = self.symbol.upper().strip()
        self.source_type = self.source_type.strip().lower()
        self.source_name = self.source_name.strip().lower()
        self.horizon = self.horizon.strip().lower()
        self.timestamp = ensure_utc(self.timestamp)
        self.strength = clip_decimal(d(self.strength), DECIMAL_ZERO, DECIMAL_ONE)
        self.confidence = clip_decimal(d(self.confidence), DECIMAL_ZERO, DECIMAL_ONE)

        if self.direction not in (-1, 0, 1):
            raise ValueError(f"direction must be -1, 0, or 1, got {self.direction}")

        if self.horizon not in {"short", "medium", "long"}:
            raise ValueError(f"unsupported horizon: {self.horizon}")

        if self.source_type not in {"quant", "news", "macro", "cross_asset"}:
            raise ValueError(f"unsupported source_type: {self.source_type}")

        if self.half_life_minutes is not None and self.half_life_minutes <= 0:
            raise ValueError("half_life_minutes must be positive when provided")


@dataclass(slots=True)
class NetSignal:
    symbol: str
    score: Decimal
    confidence: Decimal
    direction: str
    horizon_bias: str
    aligned_sources: list[str]
    conflicting_sources: list[str]
    updated_at: datetime
    components: dict[str, Decimal] = field(default_factory=dict)


@dataclass(slots=True)
class AssetSignalState:
    symbol: str

    short_score: Decimal = DECIMAL_ZERO
    medium_score: Decimal = DECIMAL_ZERO
    long_score: Decimal = DECIMAL_ZERO

    last_update: datetime = field(default_factory=utc_now)
    last_signal_at: datetime = field(default_factory=utc_now)

    source_names_seen: set[str] = field(default_factory=set)
    source_types_seen: set[str] = field(default_factory=set)
    aligned_sources: set[str] = field(default_factory=set)
    conflicting_sources: set[str] = field(default_factory=set)

    def snapshot(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "short_score": str(self.short_score),
            "medium_score": str(self.medium_score),
            "long_score": str(self.long_score),
            "last_update": self.last_update.isoformat(),
            "last_signal_at": self.last_signal_at.isoformat(),
            "source_names_seen": sorted(self.source_names_seen),
            "source_types_seen": sorted(self.source_types_seen),
            "aligned_sources": sorted(self.aligned_sources),
            "conflicting_sources": sorted(self.conflicting_sources),
        }


# Macro regime -> (direction, base_strength) before macro_confidence scaling
_MACRO_REGIME_MAP: dict[str, tuple[int, Decimal]] = {
    "risk_on_disinflation": (1, Decimal("0.5")),
    "risk_off_stagflation": (-1, Decimal("0.55")),
    "tightening": (-1, Decimal("0.4")),
    "easing": (1, Decimal("0.4")),
    "neutral": (0, Decimal("0.15")),
}


class SignalAccumulator:
    """
    Time-decayed multi-horizon conviction per symbol.
    """

    DEFAULT_SOURCE_WEIGHTS: dict[str, Decimal] = {
        "quant": Decimal("1.00"),
        "news": Decimal("0.65"),
        "macro": Decimal("0.85"),
        "cross_asset": Decimal("0.75"),
    }

    DEFAULT_HALF_LIVES_MINUTES: dict[str, int] = {
        "short": 90,
        "medium": 1440,
        "long": 10080,
    }

    HORIZON_WEIGHTS: dict[str, Decimal] = {
        "short": Decimal("0.50"),
        "medium": Decimal("0.30"),
        "long": Decimal("0.20"),
    }

    LONG_THRESHOLD = Decimal("0.20")
    SHORT_THRESHOLD = Decimal("-0.20")
    ALIGNMENT_BONUS = Decimal("0.15")
    MAX_CONFLICT_PENALTY = Decimal("0.40")
    STALE_RESET_MINUTES = 60 * 24 * 14

    def __init__(
        self,
        source_weights: dict[str, Decimal] | None = None,
        half_lives_minutes: dict[str, int] | None = None,
        horizon_weights: dict[str, Decimal] | None = None,
        stale_reset_minutes: int | None = None,
    ) -> None:
        self._states: dict[str, AssetSignalState] = {}
        self.source_weights = dict(source_weights or self.DEFAULT_SOURCE_WEIGHTS)
        self.half_lives_minutes = dict(half_lives_minutes or self.DEFAULT_HALF_LIVES_MINUTES)
        self.horizon_weights = dict(horizon_weights or self.HORIZON_WEIGHTS)
        self.stale_reset_minutes = int(stale_reset_minutes or self.STALE_RESET_MINUTES)

    def get_or_create(self, symbol: str, now: datetime | None = None) -> AssetSignalState:
        clean_symbol = symbol.upper().strip()
        if clean_symbol not in self._states:
            ts = ensure_utc(now or utc_now())
            self._states[clean_symbol] = AssetSignalState(
                symbol=clean_symbol,
                last_update=ts,
                last_signal_at=ts,
            )
        return self._states[clean_symbol]

    def get_state(self, symbol: str) -> AssetSignalState | None:
        return self._states.get(symbol.upper().strip())

    def reset_symbol(self, symbol: str) -> None:
        self._states.pop(symbol.upper().strip(), None)

    def compute_net_for_symbol(self, symbol: str, now: datetime | None = None) -> NetSignal | None:
        state = self.get_state(symbol)
        if state is None:
            return None
        return self.compute_net_signal(state, now)

    def dashboard_snapshot(self, *, top_n: int = 10, now: datetime | None = None) -> dict[str, Any]:
        """
        JSON-serializable conviction leaderboard for the dashboard.

        Per-symbol net uses horizon scores (short/medium/long) as the decomposition shown in
        ``components`` — not separate quant/news/macro ledgers (those are blended in ``update``).
        """
        now_utc = ensure_utc(now or utc_now())
        n = max(1, min(50, int(top_n)))

        def _entry(state: AssetSignalState, net: NetSignal) -> dict[str, Any]:
            return {
                "symbol": net.symbol,
                "score": str(net.score),
                "confidence": str(net.confidence),
                "direction": net.direction,
                "horizon_bias": net.horizon_bias,
                "aligned_sources": list(net.aligned_sources),
                "conflicting_sources": list(net.conflicting_sources),
                "components": {k: str(v) for k, v in net.components.items()},
                "source_types_seen": sorted(state.source_types_seen),
                "updated_at": net.updated_at.isoformat(),
            }

        entries: list[dict[str, Any]] = []
        for state in self._states.values():
            net = self.compute_net_signal(state, now_utc)
            entries.append(_entry(state, net))

        entries.sort(key=lambda e: abs(Decimal(str(e["score"]))), reverse=True)
        top_by_magnitude = entries[:n]

        bullish = [e for e in entries if Decimal(str(e["score"])) > Decimal("0")]
        bullish.sort(key=lambda e: Decimal(str(e["score"])), reverse=True)

        bearish = [e for e in entries if Decimal(str(e["score"])) < Decimal("0")]
        bearish.sort(key=lambda e: Decimal(str(e["score"])))

        return {
            "updated_at": now_utc.isoformat(),
            "top_by_magnitude": top_by_magnitude,
            "bullish_top": bullish[:n],
            "bearish_top": bearish[:n],
        }

    def feed_ai_pipeline_result(
        self,
        result: AIPipelineResult,
        symbols: Iterable[str],
        *,
        now: datetime | None = None,
    ) -> None:
        """Ingest rolled-up news per symbol + macro bias for each monitored symbol."""
        now_utc = ensure_utc(now or utc_now())
        sym_set = {s.strip().upper() for s in symbols if s and str(s).strip()}
        for symbol in sym_set:
            score = result.news_scores.get(symbol)
            if score is not None and float(score) != 0.0:
                detail = result.news_details.get(symbol, {})
                conf_f = float(detail.get("confidence", 0.7))
                conf = clip_decimal(d(Decimal(str(conf_f))), DECIMAL_ZERO, DECIMAL_ONE)
                decay_h = int(detail.get("decay_hours", 24) or 24)
                half_life = max(30, decay_h * 60)
                direction = 0
                sf = float(score)
                if sf > 0:
                    direction = 1
                elif sf < 0:
                    direction = -1
                sig = InputSignal(
                    symbol=symbol,
                    source_type="news",
                    source_name="ai_pipeline_rollup",
                    direction=direction,
                    strength=clip_decimal(d(abs(Decimal(str(sf)))), DECIMAL_ZERO, DECIMAL_ONE),
                    confidence=conf,
                    horizon="medium",
                    timestamp=now_utc,
                    half_life_minutes=half_life,
                    metadata={"rollup_score": float(score), "decay_hours": decay_h},
                )
                self.update(sig, now_utc)

            macro_dir, macro_base = _MACRO_REGIME_MAP.get(
                (result.macro_regime or "neutral").strip().lower(),
                (0, Decimal("0.15")),
            )
            if macro_dir != 0:
                mc = clip_decimal(d(Decimal(str(result.macro_confidence))), DECIMAL_ZERO, DECIMAL_ONE)
                strength = clip_decimal(macro_base * mc, DECIMAL_ZERO, DECIMAL_ONE)
                msig = InputSignal(
                    symbol=symbol,
                    source_type="macro",
                    source_name=str(result.macro_regime or "neutral").lower(),
                    direction=macro_dir,
                    strength=strength,
                    confidence=mc,
                    horizon="long",
                    timestamp=now_utc,
                    metadata={"macro_regime": result.macro_regime},
                )
                self.update(msig, now_utc)

    def update(self, signal: InputSignal, now: datetime | None = None) -> NetSignal:
        now_utc = ensure_utc(now or signal.timestamp)
        state = self.get_or_create(signal.symbol, now_utc)

        before = state.snapshot()
        self.reset_if_stale(state, now_utc)
        after_stale_reset = state.snapshot()

        after_decay = self.apply_time_decay(state, now_utc)
        contribution = self.compute_contribution(signal)

        self.apply_signal(state, signal, contribution, now_utc)
        net_signal = self.compute_net_signal(state, now_utc)

        self._log_update(
            signal=signal,
            before=before,
            after_stale_reset=after_stale_reset,
            after_decay=after_decay,
            contribution=contribution,
            after_update=state.snapshot(),
            net_signal=net_signal,
        )
        return net_signal

    def apply_time_decay(self, state: AssetSignalState, now: datetime) -> dict[str, Any]:
        now_utc = ensure_utc(now)
        delta_minutes = self._minutes_between(state.last_update, now_utc)
        if delta_minutes <= DECIMAL_ZERO:
            return state.snapshot()

        state.short_score = self._decay_value(
            state.short_score,
            delta_minutes,
            self.half_lives_minutes["short"],
        )
        state.medium_score = self._decay_value(
            state.medium_score,
            delta_minutes,
            self.half_lives_minutes["medium"],
        )
        state.long_score = self._decay_value(
            state.long_score,
            delta_minutes,
            self.half_lives_minutes["long"],
        )

        state.last_update = now_utc
        return state.snapshot()

    def compute_contribution(self, signal: InputSignal) -> Decimal:
        source_weight = self.source_weights.get(signal.source_type, DECIMAL_ONE)
        regime_weight = clip_decimal(d(signal.metadata.get("regime_weight", "1.0")), DECIMAL_ZERO, Decimal("2.0"))

        raw = (
            d(signal.direction)
            * signal.strength
            * signal.confidence
            * source_weight
            * regime_weight
        )
        return raw

    def apply_signal(
        self,
        state: AssetSignalState,
        signal: InputSignal,
        contribution: Decimal,
        now: datetime,
    ) -> None:
        if signal.direction == 0 and contribution == DECIMAL_ZERO:
            state.last_signal_at = ensure_utc(signal.timestamp)
            state.last_update = ensure_utc(now)
            return

        if signal.horizon == "short":
            state.short_score = tanh_clip(state.short_score + contribution)
        elif signal.horizon == "medium":
            state.medium_score = tanh_clip(state.medium_score + contribution)
        elif signal.horizon == "long":
            state.long_score = tanh_clip(state.long_score + contribution)
        else:
            raise ValueError(f"unsupported horizon: {signal.horizon}")

        state.source_names_seen.add(signal.source_name)
        state.source_types_seen.add(signal.source_type)
        state.last_signal_at = ensure_utc(signal.timestamp)
        state.last_update = ensure_utc(now)

        self._refresh_alignment_sets(state, signal)

    def compute_net_signal(
        self,
        state: AssetSignalState,
        now: datetime | None = None,
    ) -> NetSignal:
        now_utc = ensure_utc(now or utc_now())

        short = state.short_score
        medium = state.medium_score
        long = state.long_score

        weighted_raw = (
            self.horizon_weights["short"] * short
            + self.horizon_weights["medium"] * medium
            + self.horizon_weights["long"] * long
        )

        alignment_bonus = self._compute_alignment_bonus(short, medium, long)
        weighted_with_bonus = weighted_raw * (DECIMAL_ONE + alignment_bonus)
        score = clip_decimal(tanh_clip(weighted_with_bonus), DECIMAL_MINUS_ONE, DECIMAL_ONE)

        conflict_penalty = self._compute_conflict_penalty(short, medium, long)
        confidence = self._compute_confidence(
            state=state,
            score=score,
            conflict_penalty=conflict_penalty,
            now=now_utc,
        )

        if score >= self.LONG_THRESHOLD:
            direction = "long"
        elif score <= self.SHORT_THRESHOLD:
            direction = "short"
        else:
            direction = "neutral"

        horizon_bias = self._compute_horizon_bias(short, medium, long)

        return NetSignal(
            symbol=state.symbol,
            score=score,
            confidence=confidence,
            direction=direction,
            horizon_bias=horizon_bias,
            aligned_sources=sorted(state.aligned_sources),
            conflicting_sources=sorted(state.conflicting_sources),
            updated_at=now_utc,
            components={
                "short_score": short,
                "medium_score": medium,
                "long_score": long,
                "weighted_raw": weighted_raw,
                "alignment_bonus": alignment_bonus,
                "conflict_penalty": conflict_penalty,
            },
        )

    def reset_if_stale(self, state: AssetSignalState, now: datetime) -> None:
        now_utc = ensure_utc(now)
        idle_minutes = self._minutes_between(state.last_signal_at, now_utc)
        if idle_minutes < d(self.stale_reset_minutes):
            return

        logger.warning(
            "signal_accumulator_state_reset symbol=%s idle_minutes=%s threshold_minutes=%s",
            state.symbol,
            idle_minutes,
            self.stale_reset_minutes,
        )
        state.short_score = DECIMAL_ZERO
        state.medium_score = DECIMAL_ZERO
        state.long_score = DECIMAL_ZERO
        state.source_names_seen.clear()
        state.source_types_seen.clear()
        state.aligned_sources.clear()
        state.conflicting_sources.clear()
        state.last_update = now_utc
        state.last_signal_at = now_utc

    def _compute_confidence(
        self,
        state: AssetSignalState,
        score: Decimal,
        conflict_penalty: Decimal,
        now: datetime,
    ) -> Decimal:
        magnitude = clip_decimal(abs(score), DECIMAL_ZERO, DECIMAL_ONE)

        same_direction = self._all_same_direction(
            state.short_score,
            state.medium_score,
            state.long_score,
        )
        alignment = DECIMAL_ONE if same_direction else Decimal("0.6")

        freshness = self._compute_freshness(state, now)
        diversity = clip_decimal(
            d(len(state.source_types_seen)) / Decimal("4"),
            DECIMAL_ZERO,
            DECIMAL_ONE,
        )

        base_confidence = (
            Decimal("0.40") * magnitude
            + Decimal("0.20") * alignment
            + Decimal("0.20") * freshness
            + Decimal("0.20") * diversity
        )

        adjusted = base_confidence * (DECIMAL_ONE - conflict_penalty)
        return clip_decimal(adjusted, DECIMAL_ZERO, DECIMAL_ONE)

    def _compute_alignment_bonus(
        self,
        short: Decimal,
        medium: Decimal,
        long: Decimal,
    ) -> Decimal:
        if self._all_same_direction(short, medium, long):
            return self.ALIGNMENT_BONUS
        return DECIMAL_ZERO

    def _compute_conflict_penalty(
        self,
        short: Decimal,
        medium: Decimal,
        long: Decimal,
    ) -> Decimal:
        conflict = abs(short - medium) + abs(medium - long) + abs(short - long)
        penalty = conflict * Decimal("0.10")
        return clip_decimal(penalty, DECIMAL_ZERO, self.MAX_CONFLICT_PENALTY)

    def _compute_horizon_bias(
        self,
        short: Decimal,
        medium: Decimal,
        long: Decimal,
    ) -> str:
        magnitudes = {
            "short": abs(short),
            "medium": abs(medium),
            "long": abs(long),
        }
        strongest = max(magnitudes, key=magnitudes.get)

        directions = {sign_of(short), sign_of(medium), sign_of(long)}
        directions.discard(0)

        if len(directions) > 1:
            return "mixed"
        return strongest

    def _compute_freshness(self, state: AssetSignalState, now: datetime) -> Decimal:
        age_minutes = self._minutes_between(state.last_signal_at, now)
        if age_minutes <= Decimal("60"):
            return DECIMAL_ONE
        if age_minutes <= Decimal("360"):
            return Decimal("0.9")
        if age_minutes <= Decimal("1440"):
            return Decimal("0.7")
        if age_minutes <= Decimal("4320"):
            return Decimal("0.5")
        return Decimal("0.3")

    def _refresh_alignment_sets(self, state: AssetSignalState, signal: InputSignal) -> None:
        if self._all_same_direction(
            state.short_score,
            state.medium_score,
            state.long_score,
        ):
            state.aligned_sources.add(signal.source_name)
            state.conflicting_sources.discard(signal.source_name)
        else:
            state.conflicting_sources.add(signal.source_name)
            state.aligned_sources.discard(signal.source_name)

    @staticmethod
    def _minutes_between(start: datetime, end: datetime) -> Decimal:
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        delta_seconds = max(0.0, (end_utc - start_utc).total_seconds())
        return d(delta_seconds) / Decimal("60")

    @staticmethod
    def _decay_value(value: Decimal, delta_minutes: Decimal, half_life_minutes: int) -> Decimal:
        if value == DECIMAL_ZERO:
            return value

        half_life = d(half_life_minutes)
        if half_life <= DECIMAL_ZERO:
            return value

        decay_factor = Decimal("0.5") ** (delta_minutes / half_life)
        return value * decay_factor

    @staticmethod
    def _all_same_direction(*values: Decimal) -> bool:
        signs = {sign_of(v) for v in values}
        signs.discard(0)
        return len(signs) == 1 and len(signs) > 0

    def _log_update(
        self,
        signal: InputSignal,
        before: dict[str, Any],
        after_stale_reset: dict[str, Any],
        after_decay: dict[str, Any],
        contribution: Decimal,
        after_update: dict[str, Any],
        net_signal: NetSignal,
    ) -> None:
        payload = {
            "event": "signal_accumulator_update",
            "symbol": signal.symbol,
            "timestamp": signal.timestamp.isoformat(),
            "input_signal": {
                "source_type": signal.source_type,
                "source_name": signal.source_name,
                "direction": signal.direction,
                "strength": str(signal.strength),
                "confidence": str(signal.confidence),
                "horizon": signal.horizon,
                "half_life_minutes": signal.half_life_minutes,
                "metadata": signal.metadata,
            },
            "before": before,
            "after_stale_reset": after_stale_reset,
            "after_decay": after_decay,
            "contribution": str(contribution),
            "after_update": after_update,
            "net_signal": {
                "score": str(net_signal.score),
                "confidence": str(net_signal.confidence),
                "direction": net_signal.direction,
                "horizon_bias": net_signal.horizon_bias,
                "aligned_sources": net_signal.aligned_sources,
                "conflicting_sources": net_signal.conflicting_sources,
                "components": {k: str(v) for k, v in net_signal.components.items()},
                "updated_at": net_signal.updated_at.isoformat(),
            },
        }
        logger.info(
            "%s",
            json.dumps(payload, sort_keys=True, default=str),
            extra={"component": "signal_accumulator", "symbol": signal.symbol},
        )


def raw_signal_to_input_signal(
    raw: Any,
    *,
    timestamp: datetime | None = None,
) -> InputSignal | None:
    """Map a RawSignal-like object to InputSignal. Returns None if side is hold/neutral."""
    side = (getattr(raw, "side", None) or "").strip().lower()
    if side in ("hold", "flat", "neutral", ""):
        return None
    if side in ("buy", "long"):
        direction = 1
    elif side in ("sell", "short"):
        direction = -1
    else:
        direction = 1

    md = getattr(raw, "metadata", None) or {}
    hz = str(md.get("signal_horizon", "short")).strip().lower()
    if hz not in {"short", "medium", "long"}:
        hz = "short"

    conf = clip_decimal(d(Decimal(str(getattr(raw, "confidence", 0)))), DECIMAL_ZERO, DECIMAL_ONE)
    ts = ensure_utc(timestamp or utc_now())

    return InputSignal(
        symbol=str(getattr(raw, "symbol", "")),
        source_type="quant",
        source_name=str(getattr(raw, "strategy", "unknown") or "unknown").lower(),
        direction=direction,
        strength=conf,
        confidence=conf,
        horizon=hz,
        timestamp=ts,
        metadata={k: v for k, v in md.items() if k != "signal_horizon"},
    )
