from __future__ import annotations

import uuid
from dataclasses import replace as dataclass_replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intelligence.trade_admission.config import load_admission_config
from intelligence.trade_admission.feature_builder import build_features
from intelligence.trade_admission.ledger import (
    insert_admission,
    label_due_outcomes,
    train_admission_model,
    update_downstream_status,
)
from intelligence.trade_admission.model import AdmissionModel
from intelligence.trade_admission.policy import decide_admission
from intelligence.trade_admission.schema import (
    AdmissionAction,
    AdmissionCandidate,
    AdmissionConfig,
    AdmissionDecision,
)


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


class TradeAdmissionService:
    """Shadow-first admission layer for executable trade candidates."""

    def __init__(self, cfg: AdmissionConfig | None = None):
        self.cfg = cfg or load_admission_config()
        self._model: AdmissionModel = AdmissionModel.empty(self.cfg.model_min_bucket_samples)

    def reload(self) -> None:
        self.cfg = load_admission_config()

    @property
    def model(self) -> AdmissionModel:
        return self._model

    async def refresh_model(self, session_factory: async_sessionmaker[AsyncSession] | None) -> dict[str, Any]:
        """Rebuild the calibrated model from matured historical outcomes."""
        if not self.cfg.enabled or not self.cfg.model_enabled:
            return {"trained": False, "reason": "disabled"}
        try:
            self._model = await train_admission_model(
                session_factory,
                lookback_days=self.cfg.model_lookback_days,
                min_samples=self.cfg.model_min_bucket_samples,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("trade_admission | model refresh failed | {}", exc)
            return {"trained": False, "error": str(exc)}
        return self._model.health()

    async def evaluate_signal(
        self,
        signal: Any,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None,
        portfolio_state: dict[str, Any] | None,
        loop_iteration: int | None,
        source_path: str,
    ) -> AdmissionDecision:
        if not self.cfg.enabled:
            return AdmissionDecision(
                action=AdmissionAction.ALLOW,
                reason="admission_disabled",
                score=None,
                uncertainty=None,
            )
        md = getattr(signal, "metadata", None)
        md = dict(md) if isinstance(md, dict) else {}
        is_reduce = bool(
            md.get("reduce_only")
            or md.get("close_only")
            or md.get("flatten_all")
            or str(md.get("coordinator_kind", "")).lower() == "trim_symbol"
        )
        row_id = str(uuid.uuid4())
        suggested_qty = _dec(getattr(signal, "suggested_quantity", None))
        suggested_price = _dec(getattr(signal, "suggested_price", None))
        suggested_notional = _dec(md.get("target_notional"))
        if suggested_notional is None and suggested_qty is not None and suggested_price is not None:
            suggested_notional = abs(suggested_qty * suggested_price)
        if "confidence" not in md and getattr(signal, "confidence", None) is not None:
            md["confidence"] = str(getattr(signal, "confidence"))
        candidate = AdmissionCandidate(
            id=row_id,
            timestamp=datetime.now(timezone.utc),
            loop_iteration=loop_iteration,
            symbol=str(getattr(signal, "symbol", "") or ""),
            strategy=str(getattr(signal, "strategy", "") or "unknown"),
            side=str(getattr(signal, "side", "") or "") or None,
            broker=str(getattr(signal, "broker", "") or "") or None,
            asset_class=str(getattr(signal, "asset_class", "") or "") or None,
            signal_id=str(getattr(signal, "signal_id", "") or "") or None,
            source_path=source_path,
            suggested_notional=suggested_notional,
            suggested_quantity=suggested_qty,
            suggested_price=suggested_price,
            is_reduce_only=is_reduce,
            metadata=md,
        )
        features = build_features(candidate, portfolio_state)
        decision = decide_admission(candidate, features, self.cfg, self._model)
        if not isinstance(getattr(signal, "metadata", None), dict):
            signal.metadata = {}
        signal.metadata["trade_admission_id"] = row_id
        signal.metadata["trade_admission_decision"] = decision.action.value
        signal.metadata["trade_admission_reason"] = decision.reason
        if decision.score is not None:
            signal.metadata["trade_admission_score"] = str(decision.score)
        if decision.uncertainty is not None:
            signal.metadata["trade_admission_uncertainty"] = str(decision.uncertainty)
        if decision.model_probability is not None:
            signal.metadata["trade_admission_model_probability"] = str(decision.model_probability)
            md["trade_admission_model_probability"] = str(decision.model_probability)
            md["trade_admission_model_samples"] = decision.model_samples
        if decision.size_multiplier is not None:
            try:
                signal.suggested_quantity = signal.suggested_quantity * decision.size_multiplier
                signal.metadata["trade_admission_size_multiplier"] = str(decision.size_multiplier)
            except Exception as exc:  # noqa: BLE001
                logger.debug("trade_admission | size haircut skipped | {}", exc)

        await insert_admission(
            session_factory,
            row_id=row_id,
            timestamp=candidate.timestamp,
            loop_iteration=loop_iteration,
            symbol=candidate.symbol,
            strategy=candidate.strategy,
            side=candidate.side,
            broker=candidate.broker,
            asset_class=candidate.asset_class,
            signal_id=candidate.signal_id,
            source_path=candidate.source_path,
            decision=decision.action.value,
            reason=decision.reason,
            shadow_only=self.cfg.shadow_only,
            active_applied=decision.active_applied,
            admission_score=decision.score,
            uncertainty=decision.uncertainty,
            suggested_notional=suggested_notional,
            suggested_quantity=suggested_qty,
            suggested_price=suggested_price,
            features=features.values,
            metadata=md,
        )
        return decision

    def should_block(self, decision: AdmissionDecision) -> bool:
        if self.cfg.shadow_only:
            return False
        if decision.action == AdmissionAction.REJECT and self.cfg.block_new_opens:
            return True
        if decision.action == AdmissionAction.DEFER and self.cfg.block_new_opens:
            return True
        return False

    def apply_live_overrides(self, overrides: dict[str, Any]) -> None:
        if not overrides:
            return
        updates: dict[str, Any] = {}
        if "directional_news_weight" in overrides:
            v = _dec(overrides.get("directional_news_weight"))
            if v is not None:
                updates["directional_news_weight"] = max(Decimal("0"), min(Decimal("1"), v))
        if updates:
            self.cfg = dataclass_replace(self.cfg, **updates)

    async def mark_status(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None,
        signal: Any,
        *,
        status: str,
        reason: str | None = None,
        execution_status: str | None = None,
    ) -> None:
        md = getattr(signal, "metadata", None)
        row_id = md.get("trade_admission_id") if isinstance(md, dict) else None
        await update_downstream_status(
            session_factory,
            str(row_id) if row_id else None,
            status=status,
            reason=reason,
            execution_status=execution_status,
        )

    async def label_due(self, session_factory: async_sessionmaker[AsyncSession] | None) -> int:
        if not self.cfg.enabled:
            return 0
        horizons = self.cfg.outcome_horizons_minutes or (60,)
        return await label_due_outcomes(
            session_factory,
            horizons_minutes=horizons,
            limit=self.cfg.max_rows_per_cycle,
        )
