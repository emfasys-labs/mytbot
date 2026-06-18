"""Tests for the D166 Phase 2 cost-model reconciliation in the edge scorecard."""
from __future__ import annotations

from scripts.report_edge_scorecard import backtest_cost_bps, cost_reconciliation


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
