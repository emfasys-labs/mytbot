from __future__ import annotations

from decimal import Decimal
from typing import Any

from brokers.base import BrokerAdapter


class TreasuryManager:
    """
    Per-venue balances and inventory for pre-funded arbitrage (no automatic transfers).
    Populated via ``refresh`` from connected broker adapters.
    """

    def __init__(self, logger: Any | None = None) -> None:
        self._logger = logger
        self._venue_balances: dict[str, dict[str, Decimal]] = {}
        self._venue_inventory: dict[str, dict[str, Decimal]] = {}

    @property
    def venue_balances(self) -> dict[str, dict[str, Decimal]]:
        return self._venue_balances

    @property
    def venue_inventory(self) -> dict[str, dict[str, Decimal]]:
        return self._venue_inventory

    async def refresh(self, broker_manager: Any) -> dict[str, Any]:
        """
        Pull balances and positions from ``broker_manager.adapters``.
        Returns a snapshot dict suitable for merging into portfolio_state.
        """
        self._venue_balances.clear()
        self._venue_inventory.clear()

        adapters: dict[str, BrokerAdapter] = getattr(broker_manager, "adapters", None) or {}
        for venue, adapter in adapters.items():
            v = venue.strip().lower()
            try:
                bals = await adapter.get_balance()
            except Exception as exc:  # noqa: BLE001
                if self._logger:
                    self._logger.warning("treasury | get_balance failed | {} | {}", v, exc)
                continue
            vb: dict[str, Decimal] = {}
            for b in bals:
                ccy = (b.currency or "").strip().upper()
                if ccy:
                    vb[ccy] = b.available
            if vb:
                self._venue_balances[v] = vb

            try:
                pos_list = await adapter.get_positions()
            except Exception as exc:  # noqa: BLE001
                if self._logger:
                    self._logger.debug("treasury | get_positions failed | {} | {}", v, exc)
                continue
            inv: dict[str, Decimal] = {}
            for p in pos_list:
                sym = (p.symbol or "").strip().upper()
                if sym and p.quantity != 0:
                    inv[sym] = inv.get(sym, Decimal("0")) + p.quantity
            if inv:
                self._venue_inventory[v] = inv

        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "venue_balances": {k: {c: str(v) for c, v in row.items()} for k, row in self._venue_balances.items()},
            "venue_inventory": {k: {s: str(q) for s, q in row.items()} for k, row in self._venue_inventory.items()},
        }

    def has_balance(self, venue: str, currency: str, min_amount: Decimal) -> bool:
        row = self._venue_balances.get(venue.strip().lower(), {})
        cur = currency.strip().upper()
        avail = row.get(cur, Decimal("0"))
        return avail >= min_amount

    def has_inventory(self, venue: str, symbol: str, min_quantity: Decimal) -> bool:
        row = self._venue_inventory.get(venue.strip().lower(), {})
        u = symbol.strip().upper()
        qty = row.get(u, Decimal("0"))
        if qty >= min_quantity:
            return True
        for k, q in row.items():
            if u in k or k in u:
                if abs(q) >= min_quantity:
                    return True
        return False

    def venue_utilisation(self, venue_balances: dict[str, dict[str, Decimal]]) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for venue, wallets in venue_balances.items():
            total = sum(abs(v) for v in wallets.values())
            used = sum(v for v in wallets.values() if v > 0)
            out[venue] = (used / total) if total > 0 else Decimal("0")
        return out

    def rebalance_recommendations(self) -> list[dict[str, Any]]:
        """
        Heuristic: venues with very low USDT/USDC vs others — manual transfer only.
        """
        out: list[dict[str, Any]] = []
        usd_like = ("USDT", "USDC", "USD", "ZUSD")
        totals: dict[str, Decimal] = {}
        for venue, row in self._venue_balances.items():
            t = Decimal("0")
            for ccy, amt in row.items():
                if ccy in usd_like:
                    t += amt
            totals[venue] = t
        if len(totals) < 2:
            return out
        mean = sum(totals.values()) / Decimal(len(totals))
        for venue, t in totals.items():
            if mean > 0 and t < mean * Decimal("0.25"):
                rich = max(totals.items(), key=lambda x: x[1])[0]
                if rich != venue:
                    out.append(
                        {
                            "action": "manual_transfer_recommended",
                            "from": rich,
                            "to": venue,
                            "reason": "stablecoin_balance_low_vs_peers",
                        }
                    )
        return out

    def suggest_rebalance(
        self,
        *,
        from_venue: str,
        to_venue: str,
        currency: str,
        amount: Decimal,
    ) -> dict[str, Any]:
        return {
            "action": "manual_transfer_recommended",
            "from": from_venue,
            "to": to_venue,
            "currency": currency,
            "amount": str(amount),
        }


def merge_treasury_into_portfolio_state(portfolio: dict[str, Any], treasury: TreasuryManager) -> None:
    """Attach treasury snapshot and Decimal views for risk checks."""
    snap = treasury.snapshot()
    portfolio["venue_balances"] = snap["venue_balances"]
    portfolio["venue_inventory"] = snap["venue_inventory"]
    portfolio["venue_balances_decimal"] = {
        k: dict(row) for k, row in treasury.venue_balances.items()
    }
    portfolio["venue_inventory_decimal"] = {
        k: dict(row) for k, row in treasury.venue_inventory.items()
    }
