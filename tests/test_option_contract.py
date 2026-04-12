from decimal import Decimal

import pytest

from core.instruments import OptionContractSpec, OptionRight, parse_option_contract_from_metadata


def test_option_contract_position_key() -> None:
    spec = OptionContractSpec(
        underlying_symbol="spy",
        expiry="20260420",
        strike=Decimal("500"),
        right=OptionRight.CALL,
        multiplier=100,
    )
    assert spec.position_key() == "SPY|20260420|C|500"


def test_option_contract_from_dict_roundtrip() -> None:
    d = {
        "underlying_symbol": "SPY",
        "expiry": "20260420",
        "strike": "500",
        "right": "C",
        "multiplier": 100,
    }
    spec = OptionContractSpec.from_dict(d)
    assert spec.to_dict()["underlying_symbol"] == "SPY"
    assert spec.to_dict()["right"] == "C"


def test_parse_option_contract_from_metadata() -> None:
    meta = {"option_contract": {"underlying_symbol": "SPY", "expiry": "20260420", "strike": "1", "right": "P"}}
    spec = parse_option_contract_from_metadata(meta)
    assert spec is not None
    assert spec.right == OptionRight.PUT


def test_expiry_validation() -> None:
    with pytest.raises(ValueError):
        OptionContractSpec(
            underlying_symbol="SPY",
            expiry="bad",
            strike=Decimal("1"),
            right=OptionRight.CALL,
        ).expiry_yyyymmdd()
