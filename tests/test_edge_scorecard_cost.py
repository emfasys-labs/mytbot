"""Tests for the D166 Phase 2 cost-model reconciliation in the edge scorecard."""
from __future__ import annotations

import os
import time

import scripts.report_edge_scorecard as scorecard
from scripts.report_edge_scorecard import (
    CHURN_FILLS_PER_DAY_THRESHOLD,
    backtest_cost_bps,
    build_report,
    cost_reconciliation,
    print_symbol_churn,
)


def test_backtest_cost_bps_round_trip_is_double_per_side():
    c = backtest_cost_bps()
    assert c["per_side_bps"] == c["fee_bps"] + c["slippage_bps"]
    assert abs(c["round_trip_bps"] - 2.0 * c["per_side_bps"]) < 1e-9


def test_reconciliation_ok_when_live_cheaper():
    bt = {"round_trip_bps": 30.0}
    # fee_drag 0.0005 = 5 bps/fill; slip 3 bps/fill → live RT = 2*(5+3)=16 < 30.
    row = {"fee_drag": 0.0005, "avg_slippage_bps": 3.0, "live_closes": 12}
    rec = cost_reconciliation(row, bt)
    assert rec["live_round_trip_bps"] == 16.0
    assert rec["cost_flag"] == "OK"


def test_reconciliation_flags_too_kind():
    bt = {"round_trip_bps": 30.0}
    # fee_drag 0.0015 = 15 bps/fill; slip 10 bps/fill → live RT = 2*(15+10)=50 > 30.
    row = {"fee_drag": 0.0015, "avg_slippage_bps": 10.0, "live_closes": 12}
    rec = cost_reconciliation(row, bt)
    assert rec["live_round_trip_bps"] == 50.0
    assert rec["cost_flag"] == "BACKTEST_TOO_KIND"


def test_reconciliation_no_live_data():
    bt = {"round_trip_bps": 30.0}
    row = {"fee_drag": 0.0, "avg_slippage_bps": None, "live_closes": 0}
    rec = cost_reconciliation(row, bt)
    assert rec["cost_flag"] == "no_live_data"


def test_reconciliation_tolerance_band():
    bt = {"round_trip_bps": 30.0}
    # Live RT exactly at backtest (within 5% tolerance) → OK, not flagged.
    # fee_drag 0.0010 = 10 bps/fill; slip 5 bps/fill → live RT = 2*(10+5)=30.
    row = {"fee_drag": 0.0010, "avg_slippage_bps": 5.0, "live_closes": 5}
    rec = cost_reconciliation(row, bt)
    assert rec["live_round_trip_bps"] == 30.0
    assert rec["cost_flag"] == "OK"


def test_reconciliation_handles_negative_slippage():
    # Price improvement (negative slippage) uses magnitude.
    bt = {"round_trip_bps": 30.0}
    row = {"fee_drag": 0.0005, "avg_slippage_bps": -2.0, "live_closes": 4}
    rec = cost_reconciliation(row, bt)
    assert rec["live_slip_bps"] == 2.0


# ── D231 (P1.5) — entry-strategy attribution rows ───────────────────────────


def _verdicts(strategy: str, verdict: str, pf: float) -> dict:
    return {
        strategy: {
            "long": {
                "verdict": verdict,
                "size_multiplier": 1.0,
                "metrics": {
                    "expectancy_per_trade": 100.0,
                    "profit_factor": pf,
                    "avg_win_rate": 0.6,
                    "total_trades": 50,
                },
            }
        }
    }


def test_build_report_entry_rows_use_opening_strategy_not_exit_mechanism():
    verdicts = _verdicts("trend_following", "allowed", 1.9)
    # Exit-mechanism view: everything closed via stop_loss_monitor.
    # Entry view: opening_strategy correctly attributes it to trend_following.
    live = {
        "stop_loss_monitor": {
            "closes": 2, "realised_gross": -500.0, "win_sum": 0.0, "loss_sum": -500.0,
            "wins": 0, "losses": 2, "hold": [3600.0], "all_fills": 2,
            "fees_all": 10.0, "notional_all": 5000.0, "slippage_bps": [],
        },
        "_by_entry_strategy": {
            "trend_following": {
                "closes": 2, "realised_gross": -500.0, "win_sum": 0.0, "loss_sum": -500.0,
                "wins": 0, "losses": 2, "hold": [3600.0], "all_fills": 0,
                "fees_all": 10.0, "notional_all": 5000.0, "slippage_bps": [],
            }
        },
    }
    report = build_report(verdicts, live)
    entry = {r["strategy"]: r for r in report["entry_rows"]}
    assert entry["trend_following"]["live_closes"] == 2
    assert entry["trend_following"]["net"] == -510.0
    assert entry["trend_following"]["verdict"] == "allowed"
    # The exit-mechanism rows table is untouched — trend_following has zero
    # closes there (nothing in ``live`` was tagged strategy=trend_following;
    # it only appears because verdicts.json names it), while stop_loss_monitor
    # (the actual exit mechanism) correctly shows the 2 closes.
    exit_rows = {r["strategy"]: r for r in report["rows"]}
    assert exit_rows["trend_following"]["live_closes"] == 0
    assert exit_rows["stop_loss_monitor"]["live_closes"] == 2


def test_build_report_entry_rows_empty_when_no_attributable_closes():
    verdicts = _verdicts("mean_reversion", "allowed", 1.5)
    live = {}  # no fills at all, no _by_entry_strategy key
    report = build_report(verdicts, live)
    entry = {r["strategy"]: r for r in report["entry_rows"]}
    assert entry["mean_reversion"]["live_closes"] == 0
    assert entry["mean_reversion"]["live_pf"] == 0.0


# ── D231 (P3.7) — symbol churn instrumentation ──────────────────────────────


def test_churn_threshold_is_five():
    # The review's own churn evidence (AAPL 30/8d, ETH-USD 55/8d) is far
    # above this; 5/day is a conservative trip-wire for a 3-day-min-hold book.
    assert CHURN_FILLS_PER_DAY_THRESHOLD == 5


def test_print_symbol_churn_empty(capsys):
    print_symbol_churn([])
    out = capsys.readouterr().out
    assert "none" in out.lower()


def test_print_symbol_churn_lists_rows(capsys):
    rows = [
        {
            "broker": "ibkr", "symbol": "AAPL", "day": "2026-07-02",
            "fill_count": 30, "realised": -1902.36, "fees": 612.40, "net": -2514.76,
        }
    ]
    print_symbol_churn(rows)
    out = capsys.readouterr().out
    assert "AAPL" in out
    assert "30" in out
    assert "-2,514.76" in out


# ── D231 (P3.8) — verdicts_staleness() ──────────────────────────────────────


def test_verdicts_staleness_none_when_fresh(tmp_path, monkeypatch):
    verdicts_path = tmp_path / "v.json"
    verdicts_path.write_text("{}", encoding="utf-8")
    cfg_path = tmp_path / "strategies.yaml"
    cfg_path.write_text("edge_gate:\n  max_verdict_age_days: 14\n", encoding="utf-8")
    monkeypatch.setattr(scorecard, "VERDICTS_PATH", verdicts_path)
    monkeypatch.setattr(scorecard, "STRATEGIES_CONFIG_PATH", cfg_path)

    assert scorecard.verdicts_staleness() is None


def test_verdicts_staleness_flags_old_file(tmp_path, monkeypatch):
    verdicts_path = tmp_path / "v.json"
    verdicts_path.write_text("{}", encoding="utf-8")
    old = time.time() - 30 * 86400
    os.utime(verdicts_path, (old, old))
    cfg_path = tmp_path / "strategies.yaml"
    cfg_path.write_text("edge_gate:\n  max_verdict_age_days: 14\n", encoding="utf-8")
    monkeypatch.setattr(scorecard, "VERDICTS_PATH", verdicts_path)
    monkeypatch.setattr(scorecard, "STRATEGIES_CONFIG_PATH", cfg_path)

    result = scorecard.verdicts_staleness()
    assert result is not None
    age_days, max_age_days = result
    assert age_days > 14
    assert max_age_days == 14


def test_verdicts_staleness_none_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(scorecard, "VERDICTS_PATH", tmp_path / "does_not_exist.json")
    monkeypatch.setattr(scorecard, "STRATEGIES_CONFIG_PATH", tmp_path / "also_missing.yaml")

    assert scorecard.verdicts_staleness() is None
