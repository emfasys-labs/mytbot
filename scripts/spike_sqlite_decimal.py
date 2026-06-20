"""
scripts/spike_sqlite_decimal.py
===============================
M11 Lite-profile spike — does Decimal survive a write/read round-trip on SQLite?

Project rule #3: prices and quantities are Decimal, never float. The Lite
(Docker-free) profile wants to run on SQLite instead of Postgres. SQLAlchemy's
generic ``Numeric`` maps to SQLite ``REAL`` (IEEE-754 float) affinity, which can
silently lose precision — unacceptable for prices/quantities.

This spike proves the gotcha and validates the fix: a ``TypeDecorator`` that
stores Decimal as ``TEXT`` and round-trips the exact value.

Run:  python scripts/spike_sqlite_decimal.py
Exit code 0 = the TEXT-backed type is exact (and the plain Numeric gotcha is
demonstrated); non-zero = the proposed fix did NOT round-trip exactly.
"""

from __future__ import annotations

import sys
from decimal import Decimal

from sqlalchemy import Column, Integer, Numeric, String, TypeDecorator, create_engine, select
from sqlalchemy.orm import Session, declarative_base

Base = declarative_base()


class DecimalText(TypeDecorator):
    """Exact Decimal storage on any backend by persisting the canonical string.

    Stored as TEXT so SQLite never coerces through float. ``asdecimal`` semantics
    are preserved: Python sees Decimal in and Decimal out.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        return format(Decimal(value), "f")  # plain notation, no exponent

    def process_result_value(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        return Decimal(value)


class Row(Base):
    __tablename__ = "spike"
    id = Column(Integer, primary_key=True)
    via_numeric = Column(Numeric(28, 12), nullable=True)   # the current model style
    via_text = Column(DecimalText(), nullable=True)        # the proposed fix


# Values chosen to break float: large notional, 8-dp crypto size, and a value
# with no exact binary representation.
CASES = [
    Decimal("12345678901.234567890123"),
    Decimal("0.00000001"),
    Decimal("9999999.99999999"),
    Decimal("0.1"),
    Decimal("70123.45"),
]


def main() -> int:
    engine = create_engine("sqlite://", future=True)  # in-memory
    Base.metadata.create_all(engine)

    with Session(engine) as s:
        for i, v in enumerate(CASES):
            s.add(Row(id=i, via_numeric=v, via_text=v))
        s.commit()

    numeric_exact = True
    text_exact = True
    print(f"{'input':<26} {'Numeric(REAL)':<26} {'DecimalText(TEXT)':<26}")
    print("-" * 80)
    with Session(engine) as s:
        for i, v in enumerate(CASES):
            r = s.execute(select(Row).where(Row.id == i)).scalar_one()
            n_ok = isinstance(r.via_numeric, Decimal) and r.via_numeric == v
            t_ok = isinstance(r.via_text, Decimal) and r.via_text == v
            numeric_exact = numeric_exact and n_ok
            text_exact = text_exact and t_ok
            print(f"{str(v):<26} {str(r.via_numeric):<26} {str(r.via_text):<26}")

    print("-" * 80)
    print(f"plain Numeric round-trips exactly: {numeric_exact}  "
          f"(if False -> confirms the SQLite float gotcha)")
    print(f"DecimalText round-trips exactly:   {text_exact}  (the proposed fix)")

    # The spike PASSES when the fix is exact. We do not fail on the Numeric
    # gotcha — demonstrating it is the point.
    return 0 if text_exact else 1


if __name__ == "__main__":
    raise SystemExit(main())
