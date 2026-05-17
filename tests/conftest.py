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

# Execution-mechanics tests paper-fill equity/forex signals with the live
# wall clock; they must not depend on whether real markets happen to be
# open when the suite runs. Disable the market-session gate by default.
# The dedicated `tests/test_market_session.py` re-enables it (module
# autouse fixture) and drives the logic with explicit timestamps.
os.environ.setdefault("MARKET_SESSION_GATE", "0")
