"""
Shared pytest fixtures (expand in M2+ for DB sessions, broker mocks).
"""

from __future__ import annotations

import os

import pytest

pytest_plugins = ("pytest_asyncio",)

# Let FastAPI TestClient hit /status, /discovery/*, etc. without the operator's
# DASHBOARD_READ_TOKEN from .env. Tests that assert read-auth behaviour must
# `monkeypatch.delenv("PYTEST_API_DISABLE_READ_MIDDLEWARE", raising=False)`.
os.environ.setdefault("PYTEST_API_DISABLE_READ_MIDDLEWARE", "1")

# Prevent Telegram lifecycle notification loops from blocking test execution
os.environ.setdefault("TELEGRAM_START_READY_TIMEOUT_SEC", "0.0")
os.environ.setdefault("TELEGRAM_STOP_READY_TIMEOUT_SEC", "0.0")

# Execution-mechanics tests paper-fill equity/forex signals with the live
# wall clock; they must not depend on whether real markets happen to be
# open when the suite runs. Disable the market-session gate by default.
# The dedicated `tests/test_market_session.py` re-enables it (module
# autouse fixture) and drives the logic with explicit timestamps.
os.environ.setdefault("MARKET_SESSION_GATE", "0")

# Crypto-adapter mechanics tests assert the raw get_balance() API path;
# they must not be short-circuited by the synthetic paper wallet. Disable
# it by default for the suite. `tests/test_paper_wallet.py` manages this
# env itself to exercise both the enabled and disabled paths.
os.environ.setdefault("CRYPTO_PAPER_WALLET", "0")
