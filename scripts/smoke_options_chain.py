"""
scripts/smoke_options_chain.py
================================
Wave 12 — operator smoke check for the options pricing + strategy
layer. Does NOT call any broker; runs entirely on synthetic inputs to
validate the math before flipping the gates.

Use ``scripts/smoke_ibkr_options.py`` for the live IBKR chain probe.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.options import (  # noqa: E402
    OptionInputs,
    black_scholes_greeks,
)
from strategies.options_directional import (  # noqa: E402
    LongCallStrategy,
    LongPutStrategy,
    OptionsDirectionalConfig,
)
from strategies.options_hedging import (  # noqa: E402
    CoveredCallStrategy,
    OptionsHedgingConfig,
    ProtectivePutStrategy,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Options layer smoke (Wave 12)")
    p.add_argument("--spot", type=float, default=100.0)
    p.add_argument("--strike", type=float, default=100.0)
    p.add_argument("--vol", type=float, default=0.30)
    p.add_argument("--dte-days", type=float, default=30.0)
    p.add_argument("--rate", type=float, default=0.05)
    return p.parse_args()


def main() -> int:
    a = _parse_args()
    t = a.dte_days / 365.0

    bs_call = black_scholes_greeks(
        OptionInputs(spot=a.spot, strike=a.strike, time_to_expiry_years=t,
                     volatility=a.vol, risk_free_rate=a.rate, is_call=True)
    )
    bs_put = black_scholes_greeks(
        OptionInputs(spot=a.spot, strike=a.strike, time_to_expiry_years=t,
                     volatility=a.vol, risk_free_rate=a.rate, is_call=False)
    )
    print(f"call price={bs_call.price:.4f}  delta={bs_call.greeks.delta:.4f}  gamma={bs_call.greeks.gamma:.5f}  vega={bs_call.greeks.vega:.4f}")
    print(f"put  price={bs_put.price:.4f}  delta={bs_put.greeks.delta:.4f}  gamma={bs_put.greeks.gamma:.5f}  vega={bs_put.greeks.vega:.4f}")

    # Demonstrate strategy outputs in research mode (force enabled).
    directional = OptionsDirectionalConfig(enabled=True, paper_only=True)
    hedging = OptionsHedgingConfig(enabled=True, paper_only=True)

    long_call = LongCallStrategy(directional).evaluate(
        underlying_symbol="SPY", spot=a.spot, strike=Decimal(str(a.strike)),
        expiry_yyyymmdd=datetime.now(timezone.utc).strftime("%Y%m%d"),
        time_to_expiry_years=t, volatility=a.vol, contracts=1,
        nav=Decimal("100000"),
    )
    print(f"long_call candidate emitted: {long_call is not None}")

    long_put = LongPutStrategy(directional).evaluate(
        underlying_symbol="SPY", spot=a.spot, strike=Decimal(str(a.strike)),
        expiry_yyyymmdd=datetime.now(timezone.utc).strftime("%Y%m%d"),
        time_to_expiry_years=t, volatility=a.vol, contracts=1,
        nav=Decimal("100000"),
    )
    print(f"long_put candidate emitted: {long_put is not None}")

    pp = ProtectivePutStrategy(hedging).evaluate(
        underlying_symbol="SPY", spot=a.spot, strike=Decimal(str(a.strike)),
        expiry_yyyymmdd=datetime.now(timezone.utc).strftime("%Y%m%d"),
        time_to_expiry_years=t, volatility=a.vol, contracts=1,
        nav=Decimal("100000"),
        holdings_by_symbol=[("SPY", Decimal("100"))],  # operator holds 100 shares
    )
    print(f"protective_put emitted (with underlying held): {pp is not None}")

    pp_naked = ProtectivePutStrategy(hedging).evaluate(
        underlying_symbol="SPY", spot=a.spot, strike=Decimal(str(a.strike)),
        expiry_yyyymmdd=datetime.now(timezone.utc).strftime("%Y%m%d"),
        time_to_expiry_years=t, volatility=a.vol, contracts=1,
        nav=Decimal("100000"),
        holdings_by_symbol=[],  # no underlying — must refuse
    )
    print(f"protective_put refused (no underlying): {pp_naked is None}")

    cc_naked = CoveredCallStrategy(hedging).evaluate(
        underlying_symbol="SPY", spot=a.spot, strike=Decimal(str(a.strike)),
        expiry_yyyymmdd=datetime.now(timezone.utc).strftime("%Y%m%d"),
        time_to_expiry_years=t, volatility=a.vol, contracts=1,
        holdings_by_symbol=[],
    )
    print(f"covered_call refused naked-call attempt: {cc_naked is None}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
