"""
tests/test_brain_shadow.py
==========================
D169 (Phase 4) — trained meta-labeller SHADOW mode + the brain shadow
scorecard (``scripts/report_brain_shadow.py``).

Covers:
  1. Shadow mode: the labeller is scored and stamped on metadata but the
     signal is NEVER dropped (``enforce=False`` always keeps).
  2. Enforce mode: a below-threshold decision still drops (``enforce=True``).
  3. Scorecard helpers: shadow-decision extraction, round-trip streak
     reconstruction, attribution to entry decisions, and the verdict gate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from models.meta_label.infer import MetaLabelDecision
from signals.engine import RawSignal, SignalEngine
import signals.trained_meta_labeler as tml

from scripts.report_brain_shadow import (
    attribute_streaks,
    build_verdict,
    extract_shadow_decisions,
    reconstruct_streaks,
)


def _engine(shadow: bool, enforce: bool) -> SignalEngine:
    eng = SignalEngine(config={"anti_churn": {"enabled": False}})
    # Force the labeller state deterministically (avoid YAML/model load).
    eng._trained_meta_cfg = object()
    eng._trained_meta_shadow = shadow
    eng._trained_meta_enforce = enforce
    return eng


def _raw() -> RawSignal:
    return RawSignal(
        strategy="trend_breakout",
        symbol="USDJPY=X",
        side="buy",
        confidence=0.6,
        broker="ibkr",
        asset_class="forex",
        metadata={},
    )


def _patch_decision(monkeypatch, *, kept: bool, prob: float) -> None:
    def _fake_eval(**_kwargs):
        return MetaLabelDecision(
            kept=kept,
            probability=prob,
            threshold=0.5,
            reason="approved" if kept else "below_threshold",
            model_name="mytbot_meta_labeler",
            model_version="0.2.0",
            feature_hash="abc",
        )

    monkeypatch.setattr(tml, "evaluate_features", _fake_eval)


# ── engine shadow semantics ──────────────────────────────────────────────────


def test_shadow_mode_scores_but_never_drops(monkeypatch) -> None:
    _patch_decision(monkeypatch, kept=False, prob=0.1)
    eng = _engine(shadow=True, enforce=False)
    md: dict = {}
    keep = eng._apply_trained_meta_label(
        _raw(), adjusted_confidence=0.6, news_score=None, net=None, md=md, enforce=False
    )
    assert keep is True  # shadow never drops
    assert md["meta_label_shadow"] is True
    assert md["meta_label_kept"] is False  # but records what it WOULD have done
    assert md["meta_label_probability"] == pytest.approx(0.1)


def test_enforce_mode_drops_below_threshold(monkeypatch) -> None:
    _patch_decision(monkeypatch, kept=False, prob=0.1)
    eng = _engine(shadow=False, enforce=True)
    md: dict = {}
    keep = eng._apply_trained_meta_label(
        _raw(), adjusted_confidence=0.6, news_score=None, net=None, md=md, enforce=True
    )
    assert keep is False  # enforce drops
    assert md["meta_label_shadow"] is False
    assert md["meta_label_kept"] is False


def test_no_labeller_config_is_passthrough() -> None:
    eng = SignalEngine(config={"anti_churn": {"enabled": False}})
    assert eng._trained_meta_cfg is None
    md: dict = {}
    keep = eng._apply_trained_meta_label(
        _raw(), adjusted_confidence=0.6, news_score=None, net=None, md=md, enforce=True
    )
    assert keep is True
    assert "meta_label_shadow" not in md


# ── scorecard helpers ────────────────────────────────────────────────────────


def test_extract_shadow_decisions_filters_passthrough() -> None:
    t = datetime(2026, 6, 18, tzinfo=timezone.utc)
    rows = [
        ("AAA", "buy", t, {"meta_label_shadow": True, "meta_label_kept": True, "meta_label_probability": 0.7}),
        ("BBB", "sell", t, {"meta_label_shadow": True, "meta_label_kept": False, "meta_label_probability": 0.2}),
        # passthrough (no probability) → ignored
        ("CCC", "buy", t, {"meta_label_shadow": True, "meta_label_probability": None}),
        # not a shadow row → ignored
        ("DDD", "buy", t, {"meta_label_kept": True, "meta_label_probability": 0.9}),
    ]
    out = extract_shadow_decisions(rows)
    assert [d["symbol"] for d in out] == ["AAA", "BBB"]
    assert out[0]["side"] == "long" and out[0]["kept"] is True
    assert out[1]["side"] == "short" and out[1]["kept"] is False


def test_reconstruct_streaks_open_to_flat() -> None:
    t0 = datetime(2026, 6, 18, tzinfo=timezone.utc)
    # open long (qty 10), then close to flat at +100 realised.
    fills = [
        ("ibkr", "AAA", t0, 0.0, 10.0, 1000.0),
        ("ibkr", "AAA", t0 + timedelta(hours=1), 100.0, 0.0, 1000.0),
        # a separate later streak that stays open
        ("ibkr", "AAA", t0 + timedelta(hours=2), 0.0, -5.0, 500.0),
    ]
    streaks = reconstruct_streaks(fills)
    closed = [s for s in streaks if s["closed"]]
    assert len(closed) == 1
    assert closed[0]["direction"] == "long"
    assert closed[0]["realised_pnl"] == pytest.approx(100.0)
    assert any(not s["closed"] for s in streaks)


def test_attribute_and_verdict_re_admit() -> None:
    t0 = datetime(2026, 6, 18, tzinfo=timezone.utc)
    decisions = []
    fills = []
    # 8 would-DROP entries that each lose 50, 8 would-KEEP that each make 50.
    for i in range(8):
        ts = t0 + timedelta(hours=i)
        sym = f"DROP{i}"
        decisions.append({"symbol": sym, "side": "long", "ts": ts, "kept": False, "probability": 0.2})
        fills.append(("ibkr", sym, ts, 0.0, 10.0, 1000.0))
        fills.append(("ibkr", sym, ts + timedelta(minutes=30), -50.0, 0.0, 1000.0))
    for i in range(8):
        ts = t0 + timedelta(hours=i)
        sym = f"KEEP{i}"
        decisions.append({"symbol": sym, "side": "long", "ts": ts, "kept": True, "probability": 0.8})
        fills.append(("ibkr", sym, ts, 0.0, 10.0, 1000.0))
        fills.append(("ibkr", sym, ts + timedelta(minutes=30), 50.0, 0.0, 1000.0))

    streaks = reconstruct_streaks(fills)
    attribution = attribute_streaks(decisions, streaks)
    assert attribution["drop"]["count"] == 8
    assert attribution["keep"]["count"] == 8
    assert attribution["drop"]["net_realised"] == pytest.approx(-400.0)
    assert attribution["keep"]["net_realised"] == pytest.approx(400.0)

    verdict = build_verdict(attribution, min_sample=8)
    assert verdict["verdict"] == "RE-ADMIT"


def test_verdict_do_not_admit_when_drops_are_winners() -> None:
    t0 = datetime(2026, 6, 18, tzinfo=timezone.utc)
    decisions, fills = [], []
    for i in range(8):
        ts = t0 + timedelta(hours=i)
        for tag, kept, pnl in (("DROP", False, 60.0), ("KEEP", True, 40.0)):
            sym = f"{tag}{i}"
            decisions.append({"symbol": sym, "side": "long", "ts": ts, "kept": kept, "probability": 0.5})
            fills.append(("ibkr", sym, ts, 0.0, 10.0, 1000.0))
            fills.append(("ibkr", sym, ts + timedelta(minutes=30), pnl, 0.0, 1000.0))
    attribution = attribute_streaks(decisions, reconstruct_streaks(fills))
    verdict = build_verdict(attribution, min_sample=8)
    assert verdict["verdict"] == "DO_NOT_ADMIT"


def test_verdict_insufficient_data() -> None:
    attribution = {
        "keep": {"count": 2, "net_realised": 10.0, "wins": 2, "losses": 0, "avg_probability": 0.7},
        "drop": {"count": 1, "net_realised": -5.0, "wins": 0, "losses": 1, "avg_probability": 0.2},
        "unattributed_streaks": 0,
        "decisions": 3,
    }
    verdict = build_verdict(attribution, min_sample=8)
    assert verdict["verdict"] == "INSUFFICIENT_DATA"
