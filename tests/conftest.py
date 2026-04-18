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
