"""Trade Admission Intelligence.

This package decides, audits, and later evaluates whether a proposed trade was
worth letting through to the risk engine. Defaults are shadow-only.
"""

from intelligence.trade_admission.service import TradeAdmissionService

__all__ = ["TradeAdmissionService"]

