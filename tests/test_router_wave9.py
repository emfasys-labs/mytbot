"""Wave 9: fee-prior fusion, slippage percentiles, exec metrics export/import."""

from execution.router import FEE_PRIOR_SCORE, SmartOrderRouter


class _Permissive:
    def check_permission(self, broker: str, asset_class: str) -> bool:  # noqa: ARG002
        return True

    def get_fallback_broker(self, asset_class: str, candidates, exclude):  # noqa: ARG002
        for c in candidates:
            if c not in exclude:
                return c
        return None


def test_fused_score_matches_fee_prior_with_zero_observations() -> None:
    r = SmartOrderRouter(["binance"])
    r.permissions = _Permissive()
    fused = r.fused_routing_score("binance", "BTC-USD")
    prior = float(FEE_PRIOR_SCORE.get("binance", 0.0))
    assert abs(fused - prior) < 1e-9


def test_slippage_percentiles_and_fill_rate_in_export() -> None:
    r = SmartOrderRouter(["binance", "kraken"])
    r.permissions = _Permissive()
    for slip in (1.0, 3.0, 5.0, 9.0, 2.0):
        r.record_execution_feedback(
            broker="binance", symbol="ETH-USD", filled=True, slippage_bps=slip
        )
    r.record_execution_feedback(broker="binance", symbol="ETH-USD", filled=False, slippage_bps=40.0)
    state = r.export_quality_state()
    st = (state.get("quality_stats") or {}).get("ETH-USD", {}).get("binance", {})
    assert st.get("p50_slippage_bps", 0) > 0
    assert st.get("p90_slippage_bps", 0) >= st.get("p50_slippage_bps", 0)
    assert st.get("exec_attempts", 0) == 6
    assert st.get("fill_rate", 0) < 1.0
    rows = state.get("broker_comparison") or []
    assert isinstance(rows, list) and len(rows) >= 1
    eth_rows = [x for x in rows if x.get("symbol") == "ETH-USD"]
    assert eth_rows and eth_rows[0].get("fill_rate", 1) < 1.0


def test_exec_metrics_roundtrip_via_import() -> None:
    r = SmartOrderRouter(["binance"])
    r.permissions = _Permissive()
    r.record_execution_feedback(broker="binance", symbol="XRP-USD", filled=True, slippage_bps=4.0)
    blob = r.export_quality_state()
    r2 = SmartOrderRouter(["binance"])
    r2.permissions = _Permissive()
    r2.import_quality_state(blob)
    st2 = r2.export_quality_state()
    em = (st2.get("exec_metrics") or {}).get("XRP-USD", {}).get("binance", {})
    assert em.get("attempts") == 1
    slips = em.get("slips") or []
    assert 4.0 in slips or abs(float(slips[0]) - 4.0) < 1e-6
