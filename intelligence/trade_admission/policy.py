from __future__ import annotations

from decimal import Decimal
from typing import Any

from intelligence.trade_admission.model import AdmissionModel
from intelligence.trade_admission.schema import (
    AdmissionAction,
    AdmissionCandidate,
    AdmissionConfig,
    AdmissionDecision,
    AdmissionFeatures,
)


def _d(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        x = Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None
    return x if x.is_finite() else None


def _enforce(cfg: AdmissionConfig, action: AdmissionAction) -> tuple[AdmissionAction, bool]:
    """Resolve whether an adverse decision is actually applied live.

    In shadow mode (or with the matching enforcement flag off) the *decision*
    is still recorded for the scoreboard, but the action is softened so the
    service never blocks. Returns ``(effective_action, active_applied)``.
    """
    if cfg.shadow_only:
        return action, False
    if action in (AdmissionAction.REJECT, AdmissionAction.DEFER, AdmissionAction.CLOSE_ONLY):
        if cfg.block_new_opens:
            return action, True
        return action, False
    if action == AdmissionAction.ALLOW_SMALLER:
        if cfg.allow_size_haircuts:
            return action, True
        return action, False
    return action, False


def decide_admission(
    candidate: AdmissionCandidate,
    features: AdmissionFeatures,
    cfg: AdmissionConfig,
    model: AdmissionModel | None = None,
) -> AdmissionDecision:
    vals = features.values
    if candidate.is_reduce_only:
        return AdmissionDecision(
            action=AdmissionAction.ALLOW,
            reason="reduce_only_preserved",
            score=Decimal("1"),
            uncertainty=Decimal("0"),
            features=features,
        )

    # Book-state gate: when the book is in close-only/drawdown retreat, a NEW
    # open is out of policy but reductions remain allowed → CLOSE_ONLY.
    if vals.get("close_only_book"):
        action, applied = _enforce(cfg, AdmissionAction.CLOSE_ONLY)
        return AdmissionDecision(
            action=action,
            reason="book_close_only",
            score=Decimal("0"),
            uncertainty=Decimal("0"),
            active_applied=applied,
            features=features,
        )

    explicit_bad = []
    if vals.get("meta_label_kept") is False and vals.get("meta_label_shadow") is not True:
        explicit_bad.append("prior_trade_filter_drop")
    micro = str(vals.get("microstructure_label") or "").strip().lower()
    if micro in {"high_risk", "broken", "unavailable"}:
        explicit_bad.append(f"microstructure_{micro}")
    if vals.get("execution_gated"):
        explicit_bad.append(str(vals.get("execution_gated")))
    if explicit_bad:
        action, applied = _enforce(cfg, AdmissionAction.REJECT)
        # If enforcement is off, surface the softer DEFER for the scoreboard.
        if not applied and not cfg.shadow_only:
            action = AdmissionAction.DEFER
        elif cfg.shadow_only:
            action = AdmissionAction.DEFER
        return AdmissionDecision(
            action=action,
            reason=";".join(explicit_bad),
            score=Decimal("0"),
            uncertainty=Decimal("0.25"),
            active_applied=applied,
            features=features,
        )

    news_component = _d(vals.get("news_abs"))
    directional_news = _d(vals.get("news_directional"))
    if directional_news is not None:
        weight = max(Decimal("0"), min(Decimal("1"), _d(cfg.directional_news_weight) or Decimal("0")))
        if news_component is None:
            news_component = directional_news
        else:
            news_component = ((Decimal("1") - weight) * news_component) + (weight * directional_news)
    directional_multiplier = None
    if cfg.allow_size_haircuts and not cfg.shadow_only and directional_news is not None:
        weight = max(Decimal("0"), min(Decimal("1"), _d(cfg.directional_news_weight) or Decimal("0")))
        directional_multiplier = Decimal("1") - (weight * (Decimal("1") - directional_news))
        directional_multiplier = max(Decimal("0"), min(Decimal("1"), directional_multiplier))

    parts = [
        _d(vals.get("confidence")),
        _d(vals.get("quality")),
        _d(vals.get("accumulator")),
        news_component,
        _d(vals.get("volume_component")),
    ]
    present = [p for p in parts if p is not None]
    if not present:
        return AdmissionDecision(
            action=AdmissionAction.REQUIRE_MORE_EVIDENCE,
            reason="no_admission_evidence",
            score=None,
            uncertainty=Decimal("1"),
            features=features,
        )
    score = sum(present, Decimal("0")) / Decimal(len(present))
    uncertainty = Decimal("1") - features.coverage

    # The first active version should only become restrictive after it has
    # enough local context. In shadow mode this produces a useful label without
    # changing runtime behavior.
    min_coverage = Decimal("2") / Decimal(len(parts))
    if features.coverage < min_coverage:
        return AdmissionDecision(
            action=AdmissionAction.REQUIRE_MORE_EVIDENCE,
            reason="thin_evidence",
            score=score,
            uncertainty=uncertainty,
            features=features,
        )

    # Calibrated-model overlay: if like candidates have a materially
    # below-average win-rate (more than one standard error below the global
    # base rate), reduce or refuse. The band is distribution-derived.
    if model is not None and cfg.model_enabled:
        ms = model.evaluate(
            strategy=candidate.strategy,
            asset_class=candidate.asset_class,
            score=score,
        )
        if not ms.abstain:
            floor = ms.base_rate - ms.margin
            if ms.probability < floor:
                action, applied = _enforce(cfg, AdmissionAction.REJECT)
                if not applied and not cfg.shadow_only:
                    action = AdmissionAction.DEFER
                elif cfg.shadow_only:
                    action = AdmissionAction.DEFER
                return AdmissionDecision(
                    action=action,
                    reason=f"model_below_base|p={ms.probability:.3f}<{floor:.3f}|n={ms.samples}",
                    score=score,
                    uncertainty=uncertainty,
                    active_applied=applied,
                    model_probability=ms.probability,
                    model_samples=ms.samples,
                    features=features,
                )
            if ms.probability < ms.base_rate and cfg.allow_size_haircuts:
                # Marginal: below average but within the noise band → haircut,
                # sized by how far below the base rate it sits.
                action, applied = _enforce(cfg, AdmissionAction.ALLOW_SMALLER)
                ratio = (ms.probability / ms.base_rate) if ms.base_rate > 0 else Decimal("1")
                multiplier = max(Decimal("0"), min(Decimal("1"), ratio))
                if directional_multiplier is not None:
                    multiplier = min(multiplier, directional_multiplier)
                return AdmissionDecision(
                    action=action,
                    reason=f"model_marginal|p={ms.probability:.3f}<base={ms.base_rate:.3f}",
                    score=score,
                    uncertainty=uncertainty,
                    active_applied=applied,
                    size_multiplier=multiplier if applied else None,
                    model_probability=ms.probability,
                    model_samples=ms.samples,
                    features=features,
                )
            if directional_multiplier is not None and directional_multiplier < Decimal("1"):
                return AdmissionDecision(
                    action=AdmissionAction.ALLOW_SMALLER,
                    reason="directional_news_size_adjustment",
                    score=score,
                    uncertainty=uncertainty,
                    active_applied=True,
                    size_multiplier=directional_multiplier,
                    model_probability=ms.probability,
                    model_samples=ms.samples,
                    features=features,
                )
            return AdmissionDecision(
                action=AdmissionAction.ALLOW,
                reason="admission_shadow_ok" if cfg.shadow_only else "admission_model_ok",
                score=score,
                uncertainty=uncertainty,
                model_probability=ms.probability,
                model_samples=ms.samples,
                features=features,
            )

    # Heuristic fallback (no model / model abstains): keep the original
    # uncertainty-driven haircut behaviour.
    if cfg.allow_size_haircuts and not cfg.shadow_only and uncertainty > (Decimal("1") - min_coverage):
        min_multiplier = Decimal("1") / Decimal(len(parts) * 2)
        return AdmissionDecision(
            action=AdmissionAction.ALLOW_SMALLER,
            reason="uncertain_size_haircut",
            score=score,
            uncertainty=uncertainty,
            active_applied=True,
            size_multiplier=max(min_multiplier, Decimal("1") - uncertainty),
            features=features,
        )

    if directional_multiplier is not None:
        if directional_multiplier < Decimal("1"):
            return AdmissionDecision(
                action=AdmissionAction.ALLOW_SMALLER,
                reason="directional_news_size_adjustment",
                score=score,
                uncertainty=uncertainty,
                active_applied=True,
                size_multiplier=directional_multiplier,
                features=features,
            )

    return AdmissionDecision(
        action=AdmissionAction.ALLOW,
        reason="admission_shadow_ok" if cfg.shadow_only else "admission_ok",
        score=score,
        uncertainty=uncertainty,
        features=features,
    )
