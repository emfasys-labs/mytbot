"""
Lock the public surface of ``storage.db``. Any rename or removal here cascades
into ``system/orchestrator.py`` (and the trading-loop pool reuse path) and
caused 5 consecutive startup failures on 2026-05-25 before the import was
restored. Test exists to ensure that regression cannot reappear silently.
"""

from __future__ import annotations


def test_storage_db_session_factory_exports() -> None:
    from storage import db

    for name in (
        "bind_app_database",
        "clear_app_database_bind",
        "get_app_database",
        "get_session_factory",
    ):
        assert hasattr(db, name), f"storage.db missing required export: {name}"
        assert callable(getattr(db, name)), f"storage.db.{name} must be callable"


def test_get_session_factory_returns_none_before_bind() -> None:
    from storage.db import clear_app_database_bind, get_session_factory

    clear_app_database_bind()
    assert get_session_factory() is None


def test_orchestrator_imports_get_session_factory() -> None:
    # The orchestrator path that broke at 2026-05-25 17:47.
    from storage.db import get_session_factory  # noqa: F401
