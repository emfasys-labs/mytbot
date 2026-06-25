from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class TunableParam:
    """One parameter the tuner is allowed to move, with hard bounds.

    ``name`` may be dotted (e.g. ``gross_target_pct.trader``) to address a
    nested key within its namespace's config block. ``namespace`` selects where
    the live value is injected (e.g. ``portfolio_orchestrator``).
    """

    name: str
    namespace: str
    min_value: Decimal
    max_value: Decimal
    step: Decimal
    regime_conditioned: bool = True

    @property
    def key(self) -> str:
        return f"{self.namespace}.{self.name}"

    def clamp(self, v: Decimal) -> Decimal:
        return max(self.min_value, min(self.max_value, v))


@dataclass(frozen=True)
class TunerConfig:
    enabled: bool = True
    apply_every_n_cycles: int = 20
    attribution_window_hours: float = 6.0
    exploration_rate: Decimal = Decimal("0.15")
    min_samples_to_exploit: int = 8
    regime_conditioned: bool = True
    state_path: str = "data/state/adaptive_tuner_state.json"
    ai_advisor_enabled: bool = True
    ai_min_cycles_between_calls: int = 5
    max_recent_proposals: int = 50
    params: tuple[TunableParam, ...] = ()


@dataclass
class TuningProposal:
    param_key: str
    regime: str
    old_value: Decimal
    new_value: Decimal
    source: str               # "exploit" / "explore" / "ai_guided" / "hold"
    reward_attributed: Decimal
    rationale: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
