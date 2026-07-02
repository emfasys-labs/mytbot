"""Calibrated admission model.

Deliberately boring and inspectable (per the D196 design): it learns nothing
more than the smoothed historical win-rate of *like* candidates, bucketed by
``(strategy, asset_class, evidence_band)``. There are no trained weights and no
static market thresholds — the only inputs are this system's own past
admission rows and their matured outcomes, and the decision band is derived
from the observed outcome distribution (binomial standard error of the base
rate). Buckets with too little evidence abstain so the policy falls back to the
heuristic rather than acting on noise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from intelligence.trade_admission.schema import ModelScore

# Laplace smoothing (Beta(1, 1)) — keeps a 1-sample bucket from reading as 0% or
# 100%. Not a market threshold: a fixed, conventional prior.
_ALPHA = Decimal("1")
_BETA = Decimal("1")


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v if v.is_finite() else None
    try:
        d = Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None
    return d if d.is_finite() else None


@dataclass(frozen=True)
class _Bucket:
    wins: int = 0
    losses: int = 0
    positive_return_sum: Decimal = Decimal("0")
    negative_return_abs_sum: Decimal = Decimal("0")

    @property
    def n(self) -> int:
        return self.wins + self.losses

    def rate(self) -> Decimal:
        denom = Decimal(self.wins) + Decimal(self.losses) + _ALPHA + _BETA
        return (Decimal(self.wins) + _ALPHA) / denom

    def outcome_sizing(self) -> tuple[Decimal, Decimal]:
        """Return expected outcome and its continuous capital multiplier."""
        probability = self.rate()
        average_win = (
            self.positive_return_sum / Decimal(self.wins)
            if self.wins > 0
            else Decimal("0")
        )
        average_loss = (
            self.negative_return_abs_sum / Decimal(self.losses)
            if self.losses > 0
            else Decimal("0")
        )
        expected = (probability * average_win) - (
            (Decimal("1") - probability) * average_loss
        )
        gross_reward = probability * average_win
        if expected <= 0 or gross_reward <= 0:
            # Matured negative expectancy is evidence to stop allocating new
            # capital, not a reason to manufacture a stream of ever-smaller
            # "exploration" orders. Existing positions continue to mature and
            # can improve the bucket naturally; no paid churn is required.
            return expected, Decimal("0")
        return expected, min(Decimal("1"), expected / gross_reward)


@dataclass
class AdmissionModel:
    """Immutable snapshot of bucketed win-rates, safe to share across requests."""

    buckets: dict[str, _Bucket] = field(default_factory=dict)
    pooled_buckets: dict[str, _Bucket] = field(default_factory=dict)
    score_terciles: tuple[Decimal, Decimal] | None = None
    global_wins: int = 0
    global_losses: int = 0
    min_samples: int = 25
    trained_rows: int = 0

    # ---- construction -------------------------------------------------

    @staticmethod
    def empty(min_samples: int = 25) -> "AdmissionModel":
        return AdmissionModel(min_samples=max(1, int(min_samples)))

    @classmethod
    def from_outcomes(
        cls,
        rows: list[dict[str, Any]],
        *,
        min_samples: int = 25,
    ) -> "AdmissionModel":
        """Build from rows of ``{strategy, asset_class, score, win}``.

        ``win`` is True for a profitable matured outcome, False for a loss.
        Flat / not-executed rows should be excluded by the caller — they carry
        no win/loss signal.
        """
        scores = sorted(s for s in (_dec(r.get("score")) for r in rows) if s is not None)
        terciles: tuple[Decimal, Decimal] | None = None
        if len(scores) >= 3:
            lo = scores[len(scores) // 3]
            hi = scores[(2 * len(scores)) // 3]
            if hi > lo:
                terciles = (lo, hi)

        buckets: dict[str, dict[str, Any]] = {}
        pooled_buckets: dict[str, dict[str, Any]] = {}
        gw = gl = 0
        for r in rows:
            win = bool(r.get("win"))
            gw += 1 if win else 0
            gl += 0 if win else 1
            key = cls._bucket_key(
                str(r.get("strategy") or "unknown"),
                r.get("asset_class"),
                _dec(r.get("score")),
                terciles,
            )
            outcome_return = _dec(r.get("outcome_return"))
            if outcome_return is None:
                outcome_return = Decimal("1") if win else Decimal("-1")

            def _update_slot(store: dict[str, dict[str, Any]], slot_key: str) -> None:
                slot = store.setdefault(
                    slot_key,
                    {
                        "wins": 0,
                        "losses": 0,
                        "positive_return_sum": Decimal("0"),
                        "negative_return_abs_sum": Decimal("0"),
                    },
                )
                if win:
                    slot["wins"] += 1
                    slot["positive_return_sum"] += max(Decimal("0"), outcome_return)
                else:
                    slot["losses"] += 1
                    slot["negative_return_abs_sum"] += abs(
                        min(Decimal("0"), outcome_return)
                    )

            _update_slot(buckets, key)
            pooled_key = (
                f"{str(r.get('strategy') or 'unknown')}|"
                f"{str(r.get('asset_class') or 'unknown').lower()}|all"
            )
            _update_slot(pooled_buckets, pooled_key)
        return cls(
            buckets={k: _Bucket(**v) for k, v in buckets.items()},
            pooled_buckets={k: _Bucket(**v) for k, v in pooled_buckets.items()},
            score_terciles=terciles,
            global_wins=gw,
            global_losses=gl,
            min_samples=max(1, int(min_samples)),
            trained_rows=len(rows),
        )

    # ---- scoring ------------------------------------------------------

    @staticmethod
    def _band(score: Decimal | None, terciles: tuple[Decimal, Decimal] | None) -> str:
        if score is None or terciles is None:
            return "mid"
        lo, hi = terciles
        if score < lo:
            return "low"
        if score >= hi:
            return "high"
        return "mid"

    @classmethod
    def _bucket_key(
        cls,
        strategy: str,
        asset_class: Any,
        score: Decimal | None,
        terciles: tuple[Decimal, Decimal] | None,
    ) -> str:
        ac = str(asset_class or "unknown").lower()
        return f"{strategy}|{ac}|{cls._band(score, terciles)}"

    def base_rate(self) -> Decimal:
        denom = Decimal(self.global_wins) + Decimal(self.global_losses) + _ALPHA + _BETA
        return (Decimal(self.global_wins) + _ALPHA) / denom

    def _margin(self) -> Decimal:
        """Binomial standard error of the base rate over the global sample."""
        n = self.global_wins + self.global_losses
        if n <= 0:
            return Decimal("1")
        p = self.base_rate()
        var = p * (Decimal("1") - p) / Decimal(n)
        # Decimal has no sqrt; use the float bridge only for this scalar.
        try:
            return Decimal(str(float(var) ** 0.5))
        except Exception:  # noqa: BLE001
            return Decimal("0")

    def evaluate(
        self,
        *,
        strategy: str,
        asset_class: Any,
        score: Decimal | None,
    ) -> ModelScore:
        base = self.base_rate()
        margin = self._margin()
        if self.global_wins + self.global_losses < self.min_samples:
            # The whole model is undertrained — abstain globally.
            return ModelScore(
                probability=base,
                base_rate=base,
                margin=margin,
                samples=self.global_wins + self.global_losses,
                bucket="<global>",
                abstain=True,
            )
        key = self._bucket_key(str(strategy or "unknown"), asset_class, score, self.score_terciles)
        b = self.buckets.get(key)
        if b is None or b.n < self.min_samples:
            pooled_key = (
                f"{str(strategy or 'unknown')}|"
                f"{str(asset_class or 'unknown').lower()}|all"
            )
            pooled = self.pooled_buckets.get(pooled_key)
            if pooled is not None and pooled.n >= self.min_samples:
                key = pooled_key
                b = pooled
        if b is None or b.n < self.min_samples:
            return ModelScore(
                probability=base,
                base_rate=base,
                margin=margin,
                samples=b.n if b else 0,
                bucket=key,
                abstain=True,
            )
        expected_return, size_multiplier = b.outcome_sizing()
        return ModelScore(
            probability=b.rate(),
            base_rate=base,
            margin=margin,
            samples=b.n,
            bucket=key,
            abstain=False,
            expected_return=expected_return,
            size_multiplier=size_multiplier,
        )

    def health(self) -> dict[str, Any]:
        return {
            "trained_rows": self.trained_rows,
            "buckets": len(self.buckets),
            "pooled_buckets": len(self.pooled_buckets),
            "global_wins": self.global_wins,
            "global_losses": self.global_losses,
            "base_rate": float(self.base_rate()),
            "min_samples": self.min_samples,
            "ready": (self.global_wins + self.global_losses) >= self.min_samples,
            "active_buckets": sum(
                1
                for bucket in self.buckets.values()
                if bucket.n >= self.min_samples and bucket.outcome_sizing()[1] > 0
            ),
        }
