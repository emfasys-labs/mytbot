"""
storage/types.py
================
M11 Lite profile — Decimal-safe column type for the dual Postgres/SQLite backend.

Project rule #3: prices and quantities are Decimal, never float. On Postgres the
native ``NUMERIC`` type already round-trips Decimal exactly. On SQLite (the
Docker-free Lite profile) SQLAlchemy's ``Numeric`` falls back to ``REAL`` (IEEE
float) affinity and silently corrupts money values — proven by
``scripts/spike_sqlite_decimal.py``.

``DecimalSafe`` resolves per dialect:

- **Postgres** → native ``NUMERIC(precision, scale)`` — behaviour identical to
  the previous bare ``Numeric`` columns (no migration needed; same SQL type).
- **SQLite** → ``TEXT`` storing the canonical Decimal string, round-tripped back
  to ``Decimal`` exactly.

Known Lite limitation: SQL-side aggregates (``SUM``) and threshold comparisons on
SQLite operate via SQLite's own numeric coercion (double, ~15 significant
digits), not exact Decimal. The position-critical ``SUM(signed_quantity)`` path
is summed in Python for SQLite (see ``storage/fills_ledger.py``). For fully
Decimal-exact SQL aggregation use the Standard/Postgres profile.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Numeric, String
from sqlalchemy.types import TypeDecorator


class DecimalSafe(TypeDecorator):
    """Decimal that round-trips exactly on both Postgres (NUMERIC) and SQLite (TEXT)."""

    # Generic default; the real per-dialect type comes from load_dialect_impl.
    impl = Numeric
    cache_ok = True

    def __init__(self, precision: int | None = None, scale: int | None = None, **kw):
        self.precision = precision
        self.scale = scale
        super().__init__(**kw)

    def load_dialect_impl(self, dialect):  # noqa: ANN001
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String())
        return dialect.type_descriptor(
            Numeric(self.precision, self.scale, asdecimal=True)
        )

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        if dialect.name == "sqlite":
            # Canonical plain-notation string — exact, no float, no exponent.
            return format(Decimal(str(value)), "f")
        # Postgres: hand the driver a Decimal (matches prior Numeric behaviour).
        return value if isinstance(value, Decimal) else Decimal(str(value))

    def process_result_value(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        if dialect.name == "sqlite":
            return Decimal(value)
        return value  # Postgres already yields Decimal
