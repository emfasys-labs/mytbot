"""
models/options/
=================
Wave 12 — options pricing and risk helpers.

Public surface:

- ``black_scholes_price`` / ``black_scholes_greeks`` — Δ, Γ, ν, Θ, ρ.
- ``IVSurface`` — strike × expiry IV interpolation + cheap arbitrage
  sanity checks.
- ``check_premium_exposure`` — operator-budget gate.
- ``check_underlying_required`` — refuses covered calls / protective
  puts without the corresponding long stock position.

The strategy modules in ``strategies/options_directional.py`` and
``strategies/options_hedging.py`` consume these primitives. The
package never places orders.
"""

from models.options.greeks import (
    BlackScholesResult,
    OptionGreeks,
    OptionInputs,
    black_scholes_greeks,
    black_scholes_price,
)
from models.options.iv_surface import (
    IVPoint,
    IVSurface,
    build_iv_surface,
)
from models.options.risk import (
    OptionsRiskCheck,
    check_premium_exposure,
    check_underlying_required,
)

__all__ = [
    "BlackScholesResult",
    "IVPoint",
    "IVSurface",
    "OptionGreeks",
    "OptionInputs",
    "OptionsRiskCheck",
    "black_scholes_greeks",
    "black_scholes_price",
    "build_iv_surface",
    "check_premium_exposure",
    "check_underlying_required",
]
