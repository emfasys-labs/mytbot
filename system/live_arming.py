from __future__ import annotations

import os
from typing import Any


LIVE_ARMING_PHRASE = "I_UNDERSTAND_THIS_PLACES_REAL_ORDERS"
IBKR_LIVE_ARMING_PHRASE = "I_UNDERSTAND_IBKR_LIVE_ORDERS"
IBKR_PAPER_PORT_OVERRIDE_PHRASE = "I_UNDERSTAND_IBKR_PAPER_PORT_RISK"


class LiveArmingError(RuntimeError):
    """Raised when live or IBKR port configuration is not explicitly armed."""


def _env(name: str) -> str:
    return (os.getenv(name, "") or "").strip()


def is_live_armed() -> bool:
    return _env("MYTBOT_LIVE_ARMED") == LIVE_ARMING_PHRASE


def is_ibkr_live_armed() -> bool:
    return _env("IBKR_LIVE_ARMED") == IBKR_LIVE_ARMING_PHRASE


def validate_ibkr_port_arming(*, paper_mode: bool, port: int | str) -> None:
    try:
        p = int(port)
    except Exception as exc:  # noqa: BLE001
        raise LiveArmingError(f"IBKR_PORT must be numeric; got {port!r}") from exc

    if paper_mode:
        if p == 7496:
            raise LiveArmingError(
                "Refusing APP_ENV=paper with IBKR_PORT=7496. 7496 is the standard IBKR live port."
            )
        if p != 7497 and _env("IBKR_ALLOW_NONSTANDARD_PAPER_PORT") != IBKR_PAPER_PORT_OVERRIDE_PHRASE:
            raise LiveArmingError(
                "Refusing non-standard IBKR paper port. Set "
                f"IBKR_ALLOW_NONSTANDARD_PAPER_PORT={IBKR_PAPER_PORT_OVERRIDE_PHRASE} only after verifying the port is paper."
            )
        return

    if p != 7496:
        raise LiveArmingError("APP_ENV=live requires IBKR_PORT=7496 for an explicit live IBKR route.")
    if not is_ibkr_live_armed():
        raise LiveArmingError(f"IBKR live trading requires IBKR_LIVE_ARMED={IBKR_LIVE_ARMING_PHRASE}")


def validate_live_arming(*, paper_mode: bool, broker_configs: dict[str, dict[str, Any]]) -> None:
    ibkr_cfg = broker_configs.get("ibkr") or {}
    if ibkr_cfg:
        validate_ibkr_port_arming(paper_mode=paper_mode, port=ibkr_cfg.get("port", 7497))

    if paper_mode:
        return

    if not is_live_armed():
        raise LiveArmingError(f"APP_ENV=live requires MYTBOT_LIVE_ARMED={LIVE_ARMING_PHRASE}")
