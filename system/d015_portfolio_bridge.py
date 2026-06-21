"""Map M3/M5 portfolio dict (from ``_load_portfolio_state``) to D015 ``PortfolioState``."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, cast

from core.models_runtime import AssetClass, HeldPositionState, PortfolioState, ProfileMode, Side
from core.pnl import unrealised_pnl_account_currency


def _ac(s: str) -> AssetClass:
    x = (s or "").strip().lower()
    allowed: tuple[AssetClass, ...] = (
        "equity",
        "etf",
        "bond",
        "forex",
        "crypto",
        "future",
        "option",
        "other",
    )
    if x in allowed:
        return cast(AssetClass, x)
    return "other"


def _side_from_qty(qty: Decimal) -> Side:
    return "long" if qty >= 0 else "short"


def portfolio_dict_to_runtime_state(
    portfolio_state: dict[str, Any],
    *,
    mode: str,
    capital_pct: float = 1.0,
    now: datetime | None = None,
) -> PortfolioState:
    """
    Build allocator ``PortfolioState`` from the dict returned by ``_load_portfolio_state``.

    Uses NAV and open positions only — no synthetic cash fractions. Buying power uses
    ``tradable_capital`` so margin-style accounts are not blocked when cash is zero.
    """
    ts = now or datetime.now(timezone.utc)
    pv = Decimal(str(portfolio_state.get("portfolio_value", "0")))
    tc = Decimal(str(portfolio_state.get("tradable_capital", pv)))
    ge = Decimal(str(portfolio_state.get("current_gross_exposure", "0")))
    cash = pv - ge
    if cash < 0:
        cash = Decimal("0")
    abp = tc
    if abp <= 0 and pv > 0:
        abp = pv
    pm: ProfileMode = cast(
        ProfileMode,
        mode if mode in ("defender", "trader", "hunter") else "trader",
    )
    held: list[HeldPositionState] = []
    raw_positions = portfolio_state.get("positions") or {}
    if isinstance(raw_positions, dict):
        for sym, row in raw_positions.items():
            if not isinstance(row, dict):
                continue
            try:
                qty = Decimal(str(row.get("quantity", "0")))
            except Exception:  # noqa: BLE001
                continue
            if qty == 0:
                continue
            cur = Decimal(str(row.get("current_price", "0")))
            avg = Decimal(str(row.get("avg_entry_price", cur)))
            mv = abs(qty) * cur
            cost_basis = abs(qty) * avg
            ac = _ac(str(row.get("asset_class", "")))
            ur = unrealised_pnl_account_currency(
                symbol=str(row.get("symbol", sym)),
                asset_class=ac,
                quantity=qty,
                avg_entry_price=avg,
                current_price=cur,
            )
            ur_pct = (ur / cost_basis) if cost_basis > 0 else Decimal("0")
            broker = str(row.get("broker", "") or "").strip()[:20] or None
            held.append(
                HeldPositionState(
                    symbol=str(sym).strip()[:32],
                    asset_class=ac,
                    side=_side_from_qty(qty),
                    quantity=qty,
                    entry_price=avg,
                    current_price=cur,
                    market_value=mv,
                    notional_exposure=mv,
                    unrealised_pnl=ur,
                    unrealised_pnl_pct=ur_pct,
                    metadata={"broker": broker} if broker else {},
                )
            )

    net = sum((p.market_value for p in held if p.side == "long"), Decimal("0")) - sum(
        (p.market_value for p in held if p.side == "short"),
        Decimal("0"),
    )

    return PortfolioState(
        timestamp=ts,
        mode=pm,
        nav=pv,
        cash=cash,
        available_buying_power=abp,
        gross_exposure=ge,
        net_exposure=net,
        leverage_ratio=Decimal("1") if pv <= 0 else ge / pv,
        realised_pnl_today=Decimal(str(portfolio_state.get("daily_realized_pnl", "0"))),
        positions=held,
        metadata={
            "capital_pct": capital_pct,
            "trades_today": int(portfolio_state.get("trades_today", 0)),
        },
    )
