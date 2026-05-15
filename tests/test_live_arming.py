from __future__ import annotations

import pytest

from system.live_arming import (
    IBKR_LIVE_ARMING_PHRASE,
    LIVE_ARMING_PHRASE,
    LiveArmingError,
    validate_ibkr_port_arming,
    validate_live_arming,
)


def test_paper_refuses_ibkr_live_port() -> None:
    with pytest.raises(LiveArmingError, match="IBKR_PORT=7496"):
        validate_ibkr_port_arming(paper_mode=True, port=7496)


def test_live_requires_global_and_ibkr_arming(monkeypatch) -> None:
    cfg = {"ibkr": {"port": 7496}}

    with pytest.raises(LiveArmingError, match="IBKR live trading requires"):
        validate_live_arming(paper_mode=False, broker_configs=cfg)

    monkeypatch.setenv("IBKR_LIVE_ARMED", IBKR_LIVE_ARMING_PHRASE)
    with pytest.raises(LiveArmingError, match="MYTBOT_LIVE_ARMED"):
        validate_live_arming(paper_mode=False, broker_configs=cfg)

    monkeypatch.setenv("MYTBOT_LIVE_ARMED", LIVE_ARMING_PHRASE)
    validate_live_arming(paper_mode=False, broker_configs=cfg)


def test_paper_standard_ibkr_port_is_allowed() -> None:
    validate_live_arming(paper_mode=True, broker_configs={"ibkr": {"port": 7497}})
