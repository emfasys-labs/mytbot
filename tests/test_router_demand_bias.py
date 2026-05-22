from execution.router import SmartOrderRouter


class _Permissive:
    def check_permission(self, broker: str, asset_class: str) -> bool:  # noqa: ARG002
        return True

    def get_fallback_broker(self, asset_class: str, candidates, exclude):  # noqa: ARG002
        for c in candidates:
            if c not in exclude:
                return c
        return None


def test_router_crypto_demand_bias_prefers_binance_in_risk_on() -> None:
    r = SmartOrderRouter(["kraken", "binance", "ibkr"])
    r.permissions = _Permissive()
    b = r.route("crypto", "BTC-USDT", metadata={"demand_score": 0.8})
    assert b == "binance"


def test_router_crypto_prefers_dedicated_venues_over_alpaca_by_default() -> None:
    r = SmartOrderRouter(["alpaca", "binance", "kraken"])
    r.permissions = _Permissive()
    assert r.route("crypto", "BTC-USDT", metadata={"demand_score": 0.0}) == "binance"


def test_router_crypto_usd_pairs_prefer_kraken() -> None:
    r = SmartOrderRouter(["alpaca", "binance", "bybit", "kraken"])
    r.permissions = _Permissive()
    assert r.route("crypto", "BTC-USD", metadata={"demand_score": 0.8}) == "kraken"
    assert (
        r.route(
            "crypto",
            "BTC-USD",
            metadata={"demand_score": 0.8, "allow_usd_stablecoin_conversion": "false"},
        )
        == "kraken"
    )


def test_router_crypto_usd_pairs_fallback_when_kraken_paper_room_exhausted(monkeypatch) -> None:
    r = SmartOrderRouter(["alpaca", "binance", "bybit", "kraken"])
    r.permissions = _Permissive()

    def _room(broker: str):
        return {"kraken": 0, "binance": 50000, "bybit": 25000}.get(broker)

    monkeypatch.setattr("system.paper_wallet.venue_deploy_room", _room)

    assert r.route("crypto", "BTC-USD", metadata={"demand_score": 0.0}) == "binance"


def test_router_crypto_usd_pairs_fallback_can_use_bybit_when_binance_exhausted(monkeypatch) -> None:
    r = SmartOrderRouter(["alpaca", "binance", "bybit", "kraken"])
    r.permissions = _Permissive()

    def _room(broker: str):
        return {"kraken": 0, "binance": 0, "bybit": 25000}.get(broker)

    monkeypatch.setattr("system.paper_wallet.venue_deploy_room", _room)

    assert r.route("crypto", "BTC-USD", metadata={"demand_score": 0.0}) == "bybit"


def test_router_equity_hunter_risk_on_can_prefer_alpaca() -> None:
    r = SmartOrderRouter(["ibkr", "alpaca"])
    r.permissions = _Permissive()
    b = r.route("equity", "SPY", metadata={"profile_mode": "hunter", "demand_score": 0.7})
    assert b == "alpaca"


def test_router_learned_quality_can_shift_choice() -> None:
    r = SmartOrderRouter(["kraken", "binance"])
    r.permissions = _Permissive()
    # Baseline risk-off would prefer kraken.
    b0 = r.route("crypto", "BTC-USDT", metadata={"demand_score": -0.2})
    assert b0 in {"kraken", "binance"}
    # Learn that binance execution is better for BTC.
    for _ in range(8):
        r.record_execution_feedback(broker="binance", symbol="BTC-USDT", filled=True, slippage_bps=1.0)
    for _ in range(8):
        r.record_execution_feedback(broker="kraken", symbol="BTC-USDT", filled=False, slippage_bps=20.0)
    b1 = r.route("crypto", "BTC-USDT", metadata={"demand_score": -0.2})
    assert b1 == "binance"


def test_router_quality_state_roundtrip_and_decay() -> None:
    r = SmartOrderRouter(["binance"])
    r.permissions = _Permissive()
    for _ in range(4):
        r.record_execution_feedback(broker="binance", symbol="BTC-USD", filled=True, slippage_bps=2.0)
    state = r.export_quality_state()
    assert "quality_map" in state and "history" in state
    r2 = SmartOrderRouter(["binance"])
    r2.permissions = _Permissive()
    r2.import_quality_state(state)
    b_before = r2.route("crypto", "BTC-USD", metadata={"demand_score": 0.0})
    assert b_before == "binance"
    # Decay should not explode; score remains bounded and route still valid.
    r2.apply_decay(0.2, adaptive=True)
    state2 = r2.export_quality_state()
    assert "BTC-USD" in (state2.get("quality_map") or {})
    assert "quality_stats" in state2
    stats = (state2.get("quality_stats") or {}).get("BTC-USD", {}).get("binance", {})
    assert "ci95_half" in stats
    assert "fused_score" in stats
    assert isinstance(state2.get("broker_comparison"), list)
    assert "exec_metrics" in state2
