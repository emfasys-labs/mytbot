from __future__ import annotations

from run_m5 import _build_broker_configs, _build_parser


def test_build_broker_configs_has_expected_brokers():
    cfg = _build_broker_configs()
    assert "ibkr" in cfg
    assert "kraken" in cfg
    assert "binance" in cfg
    assert "alpaca" in cfg


def test_parser_supports_reconcile_only_flag():
    p = _build_parser()
    args = p.parse_args(["--reconcile-only"])
    assert args.reconcile_only is True

