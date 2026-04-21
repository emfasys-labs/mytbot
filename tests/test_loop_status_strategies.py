"""Unit tests for ``TradingLoop.status_dict`` strategy reporting.

The /system/status endpoint exposes ``loaded_strategies`` so the dashboard can
show the full strategy roster (including idle ones). We assert the dict
reflects both signal-level strategies and the arbitrage stack.
"""

from __future__ import annotations

from types import SimpleNamespace

from system.trading_loop.loop import TradingLoop


def _invoke(loop_like):
    """Invoke the unbound ``status_dict`` against a minimal stand-in.

    We avoid constructing a full ``TradingLoop`` (which would touch DB, config
    loaders and broker adapters) by binding the method to a ``SimpleNamespace``
    that only carries the attributes ``status_dict`` reads.
    """
    return TradingLoop.status_dict(loop_like)  # type: ignore[arg-type]


def _base(**overrides):
    base = SimpleNamespace(
        is_running=True,
        iterations=3,
        last_iteration_at=None,
        loop_interval_sec=60,
        last_error=None,
        paper_mode=True,
        capital_pct=0.5,
        _strategies={},
        _arb_stack=None,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def test_status_dict_reports_signal_strategies():
    strat_a = SimpleNamespace(name="momentum_breakout", enabled=True)
    strat_b = SimpleNamespace(name="mean_reversion", enabled=False)
    out = _invoke(_base(_strategies={"momentum_breakout": strat_a, "mean_reversion": strat_b}))
    assert out["loaded_strategies"] == [
        {"name": "momentum_breakout", "enabled": True, "kind": "signal"},
        {"name": "mean_reversion", "enabled": False, "kind": "signal"},
    ]


def test_status_dict_reports_arbitrage_stack():
    # Strategies expose no ``.name`` attr in the real codebase, so we fall
    # back to the stable human-readable labels used in strategies.yaml.
    funding = SimpleNamespace(enabled=True)
    cross = SimpleNamespace(enabled=True)
    out = _invoke(_base(_arb_stack={"funding": funding, "cross": cross}))
    names_kinds = [(s["name"], s["kind"]) for s in out["loaded_strategies"]]
    assert ("funding_rate_arbitrage", "arbitrage") in names_kinds
    assert ("cross_exchange_arbitrage", "arbitrage") in names_kinds


def test_status_dict_uses_explicit_name_attr_when_present():
    funding = SimpleNamespace(name="my_funding_strat", enabled=True)
    out = _invoke(_base(_arb_stack={"funding": funding}))
    names = [s["name"] for s in out["loaded_strategies"]]
    assert "my_funding_strat" in names


def test_status_dict_empty_when_no_strategies_loaded():
    out = _invoke(_base())
    assert out["loaded_strategies"] == []


def test_status_dict_handles_missing_enabled_attr():
    # Strategies without an ``enabled`` attribute default to True so the roster
    # still renders even if a third-party strategy omits the flag.
    bare = object()
    strategy = SimpleNamespace()  # has no 'enabled' attr
    out = _invoke(_base(_strategies={"bare": bare, "no_flag": strategy}))
    assert {"name": "bare", "enabled": True, "kind": "signal"} in out["loaded_strategies"]
    assert {"name": "no_flag", "enabled": True, "kind": "signal"} in out["loaded_strategies"]


def test_status_dict_combines_signal_and_arbitrage():
    signal = SimpleNamespace(name="momentum_breakout", enabled=True)
    funding = SimpleNamespace(enabled=True)
    out = _invoke(_base(
        _strategies={"momentum_breakout": signal},
        _arb_stack={"funding": funding},
    ))
    kinds = [s["kind"] for s in out["loaded_strategies"]]
    assert kinds.count("signal") == 1
    assert kinds.count("arbitrage") == 1
