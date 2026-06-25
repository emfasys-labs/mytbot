from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from intelligence.trade_admission.schema import AdmissionCandidate, AdmissionFeatures


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v if v.is_finite() else None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def _clamp01(v: Decimal | None) -> Decimal | None:
    if v is None:
        return None
    return max(Decimal("0"), min(Decimal("1"), v))


def build_features(candidate: AdmissionCandidate, portfolio_state: dict[str, Any] | None = None) -> AdmissionFeatures:
    md = candidate.metadata if isinstance(candidate.metadata, dict) else {}
    ps = portfolio_state if isinstance(portfolio_state, dict) else {}
    positions = ps.get("positions") if isinstance(ps.get("positions"), dict) else {}
    symbol_key = str(candidate.symbol or "").upper()
    pos = positions.get(symbol_key) or positions.get(str(candidate.symbol or "")) or {}
    if not isinstance(pos, dict):
        pos = {}

    confidence = _clamp01(_dec(md.get("confidence") or md.get("signal_confidence")))
    if confidence is None:
        confidence = _clamp01(_dec(md.get("net_conviction")))
    quality = _clamp01(_dec(md.get("trade_quality_score") or md.get("quality_score")))
    accumulator = _dec(md.get("accumulator_score") or md.get("accumulated_score"))
    if accumulator is not None:
        accumulator = _clamp01((accumulator + Decimal("1")) / Decimal("2"))
    news = _dec(md.get("news_score"))
    if news is not None:
        news = _clamp01(abs(news))
    volume_z = _dec(md.get("volume_z_score"))
    if volume_z is not None:
        volume_component = _clamp01(abs(volume_z) / (abs(volume_z) + Decimal("1")))
    else:
        volume_component = None

    target_notional = _dec(md.get("target_notional")) or candidate.suggested_notional
    nav = _dec(ps.get("portfolio_value") or ps.get("total_equity") or ps.get("cash"))
    current_qty = _dec(pos.get("quantity"))
    current_px = _dec(pos.get("current_price") or pos.get("avg_entry_price"))
    existing_notional = None
    if current_qty is not None and current_px is not None:
        existing_notional = abs(current_qty) * current_px

    notional_fraction = None
    if target_notional is not None and nav is not None and nav > 0:
        notional_fraction = abs(target_notional) / nav

    # Book-state: derive a close-only signal from risk-engine flags carried on
    # portfolio_state (kill switch / daily-loss or drawdown halt) or an explicit
    # drawdown breach (portfolio value below the high-water mark beyond the
    # engine's own breaker — no static threshold, the breaker value travels with
    # the state). When the book is in retreat, new opens are out of policy but
    # reductions stay allowed → CLOSE_ONLY.
    ps_meta = ps.get("metadata") if isinstance(ps.get("metadata"), dict) else {}
    close_only_book = bool(
        ps.get("kill_switch_active")
        or ps.get("daily_loss_halt")
        or ps.get("drawdown_halt")
        or ps.get("close_only")
        or (isinstance(ps_meta, dict) and (
            ps_meta.get("kill_switch_active")
            or ps_meta.get("close_only")
            or ps_meta.get("risk_halt")
        ))
    )
    hwm = _dec(ps.get("high_watermark_value"))
    drawdown_fraction = None
    if hwm is not None and hwm > 0 and nav is not None:
        drawdown_fraction = max(Decimal("0"), (hwm - nav) / hwm)

    values: dict[str, Any] = {
        "confidence": str(confidence) if confidence is not None else None,
        "quality": str(quality) if quality is not None else None,
        "accumulator": str(accumulator) if accumulator is not None else None,
        "news_abs": str(news) if news is not None else None,
        "volume_component": str(volume_component) if volume_component is not None else None,
        "target_notional": str(target_notional) if target_notional is not None else None,
        "notional_fraction_nav": str(notional_fraction) if notional_fraction is not None else None,
        "existing_notional": str(existing_notional) if existing_notional is not None else None,
        "has_position": bool(current_qty and current_qty != 0),
        "close_only_book": close_only_book,
        "drawdown_fraction": str(drawdown_fraction) if drawdown_fraction is not None else None,
        "is_reduce_only": bool(candidate.is_reduce_only),
        "asset_class": candidate.asset_class,
        "broker": candidate.broker,
        "strategy": candidate.strategy,
        "source_path": candidate.source_path,
        "meta_label_kept": md.get("meta_label_kept"),
        "microstructure_label": md.get("microstructure_label"),
        "execution_gated": md.get("execution_gated"),
    }
    evidence_keys = ["confidence", "quality", "accumulator", "news_abs", "volume_component"]
    present = sum(1 for k in evidence_keys if values.get(k) is not None)
    coverage = Decimal(present) / Decimal(len(evidence_keys))
    return AdmissionFeatures(values=values, coverage=coverage)
