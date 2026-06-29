"""Single owner for absolute portfolio targets across allocator paths."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from portfolio.balance import economic_key


def _dec(value: Any) -> Decimal:
    try:
        result = Decimal(str(value or 0))
    except Exception:  # noqa: BLE001
        return Decimal("0")
    return result if result.is_finite() else Decimal("0")


@dataclass(frozen=True)
class TargetClaim:
    economic_symbol: str
    signed_target: Decimal
    source: str
    feature_bar: str = ""


class PortfolioTargetLedger:
    """Coordinate primary, reserve, and natural strategy target ownership.

    A cycle may have several candidate-producing paths, but there is only one
    portfolio.  The primary allocator owns a symbol once it publishes a
    target; reserve paths may fill the remaining gap but cannot invent a
    second target.  A reduction tombstone lasts for the feature bar so a daily
    target cannot be trimmed and bought back by another path.
    """

    def __init__(self) -> None:
        self.cycle: int | None = None
        self._claims: dict[str, TargetClaim] = {}
        self._reduced_feature_bar: dict[str, str] = {}

    def begin_cycle(self, cycle: int) -> None:
        if self.cycle == int(cycle):
            return
        self.cycle = int(cycle)
        self._claims.clear()

    def claim(
        self,
        symbol: Any,
        signed_target: Any,
        *,
        source: str,
        feature_bar: Any = "",
        replace_existing: bool = False,
    ) -> TargetClaim:
        key = economic_key(symbol)
        existing = self._claims.get(key)
        if existing is not None and not replace_existing:
            return existing
        claim = TargetClaim(
            economic_symbol=key,
            signed_target=_dec(signed_target),
            source=str(source or "unknown"),
            feature_bar=str(feature_bar or ""),
        )
        self._claims[key] = claim
        return claim

    def get(self, symbol: Any) -> TargetClaim | None:
        return self._claims.get(economic_key(symbol))

    def mark_reduction(self, symbol: Any, *, feature_bar: Any = "") -> None:
        bar = str(feature_bar or "")
        if bar:
            self._reduced_feature_bar[economic_key(symbol)] = bar

    def increase_allowed(self, symbol: Any, *, feature_bar: Any = "") -> bool:
        bar = str(feature_bar or "")
        if not bar:
            return True
        return self._reduced_feature_bar.get(economic_key(symbol)) != bar

    def remaining_target(
        self,
        symbol: Any,
        *,
        intended_sign: int,
        existing_notional: Any,
        fallback_target: Any,
        source: str,
        feature_bar: Any = "",
    ) -> tuple[Decimal, TargetClaim]:
        key = economic_key(symbol)
        claim = self._claims.get(key)
        if claim is None:
            claim = self.claim(
                key,
                Decimal(intended_sign) * abs(_dec(fallback_target)),
                source=source,
                feature_bar=feature_bar,
            )
        if claim.signed_target == 0 or (
            claim.signed_target > 0
        ) != (intended_sign > 0):
            return Decimal("0"), claim
        remaining = abs(claim.signed_target) - abs(_dec(existing_notional))
        return max(Decimal("0"), remaining), claim

    def snapshot(self) -> dict[str, Any]:
        return {
            "cycle": self.cycle,
            "claims": {
                key: {
                    "signed_target": str(claim.signed_target),
                    "source": claim.source,
                    "feature_bar": claim.feature_bar,
                }
                for key, claim in self._claims.items()
            },
            "reduction_tombstones": dict(self._reduced_feature_bar),
        }
