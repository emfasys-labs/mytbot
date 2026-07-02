"""Portfolio-wide economic exposure and balance controls.

This module is intentionally pure.  It gives the allocator, runtime
invariants, and reconciliation tooling one definition of:

* an economic position (all aliases/venues summed);
* semantic factor exposure (ETF look-through and correlated themes);
* risk-aware target weights; and
* cost-aware legacy consolidation candidates.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from core.instrument_semantics import InstrumentRole, instrument_role
from portfolio.cluster_map import economic_factor_loadings, factor_overlap
from portfolio.hrp import hrp_weights

D0 = Decimal("0")
D1 = Decimal("1")


def _dec(value: Any, default: str = "0") -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal(default)
    return result if result.is_finite() else Decimal(default)


def economic_key(symbol: Any) -> str:
    from core.instrument_semantics import canonical_economic_symbol

    value = canonical_economic_symbol(symbol)
    if value.endswith("=X"):
        value = value[:-2]
    return value


@dataclass(frozen=True)
class BalancePolicy:
    enabled: bool = False
    hrp_blend: Decimal = Decimal("0.50")
    overlap_penalty: Decimal = Decimal("0.60")
    max_incremental_factor_share: Decimal = Decimal("0.25")
    min_efficiency: Decimal = Decimal("0.05")
    broker_concentration_warn: Decimal = Decimal("0.60")
    tiny_position_nav_pct: Decimal = Decimal("0.001")
    estimated_round_trip_cost_bps: Decimal = Decimal("15")
    reconciliation_enabled: bool = True
    reconciliation_auto_execute_paper: bool = True
    reconciliation_auto_execute_live: bool = False
    reconciliation_max_actions_per_cycle: int = 1
    reconciliation_min_hold_sec: Decimal = Decimal("86400")
    expected_avoided_round_trips: Decimal = Decimal("4")
    max_legacy_expressions_by_factor: dict[str, int] = field(
        default_factory=lambda: {"crypto_beta": 5}
    )
    volatility_by_asset: dict[str, Decimal] = field(
        default_factory=lambda: {
            "crypto": Decimal("0.70"),
            "equity": Decimal("0.20"),
            "forex": Decimal("0.10"),
            "bond": Decimal("0.08"),
            "cash": Decimal("0.01"),
            "other": Decimal("0.25"),
        }
    )
    base_correlation_by_asset: dict[str, Decimal] = field(
        default_factory=lambda: {
            "crypto": Decimal("0.70"),
            "equity": Decimal("0.15"),
            "forex": Decimal("0.30"),
            "bond": Decimal("0.55"),
            "cash": Decimal("0.05"),
            "other": Decimal("0.10"),
        }
    )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "BalancePolicy":
        cfg = dict(raw or {})
        kwargs: dict[str, Any] = {"enabled": bool(cfg.get("enabled", False))}
        for name in (
            "hrp_blend",
            "overlap_penalty",
            "max_incremental_factor_share",
            "min_efficiency",
            "broker_concentration_warn",
            "tiny_position_nav_pct",
            "estimated_round_trip_cost_bps",
            "expected_avoided_round_trips",
            "reconciliation_min_hold_sec",
        ):
            if cfg.get(name) is not None:
                kwargs[name] = _dec(cfg[name])
        for name in (
            "reconciliation_enabled",
            "reconciliation_auto_execute_paper",
            "reconciliation_auto_execute_live",
        ):
            if cfg.get(name) is not None:
                kwargs[name] = bool(cfg[name])
        if cfg.get("reconciliation_max_actions_per_cycle") is not None:
            try:
                kwargs["reconciliation_max_actions_per_cycle"] = max(
                    0, int(cfg["reconciliation_max_actions_per_cycle"])
                )
            except (TypeError, ValueError):
                pass
        for name in ("volatility_by_asset", "base_correlation_by_asset"):
            value = cfg.get(name)
            if isinstance(value, Mapping):
                kwargs[name] = {str(k).lower(): _dec(v) for k, v in value.items()}
        limits = cfg.get("max_legacy_expressions_by_factor")
        if isinstance(limits, Mapping):
            parsed: dict[str, int] = {}
            for key, value in limits.items():
                try:
                    parsed[str(key)] = max(1, int(value))
                except (TypeError, ValueError):
                    continue
            if parsed:
                kwargs["max_legacy_expressions_by_factor"] = parsed
        return cls(**kwargs)


def aggregate_book_positions(positions: Iterable[Any]) -> tuple[list[Any], dict[str, Any]]:
    """Sum every venue/alias row into one economic book position.

    The returned objects preserve the input dataclass type through
    ``dataclasses.replace``.  The largest venue is retained as the execution
    expression while diagnostics retain every contributing venue.
    """
    grouped: dict[str, list[Any]] = {}
    for position in positions:
        grouped.setdefault(economic_key(getattr(position, "symbol", "")), []).append(position)

    aggregated: list[Any] = []
    duplicates: list[dict[str, Any]] = []
    for key, rows in grouped.items():
        if not rows:
            continue
        signed_qty = sum((_dec(getattr(row, "signed_qty", 0)) for row in rows), D0)
        if signed_qty == 0:
            continue
        signed_cost = sum(
            (
                _dec(getattr(row, "signed_qty", 0))
                * _dec(getattr(row, "avg_price", 0))
                for row in rows
            ),
            D0,
        )
        total_abs_qty = sum((abs(_dec(getattr(row, "signed_qty", 0))) for row in rows), D0)
        current_notional = sum(
            (
                _dec(getattr(row, "signed_qty", 0))
                * _dec(getattr(row, "current_price", 0))
                for row in rows
            ),
            D0,
        )
        avg_price = abs(signed_cost / signed_qty) if signed_qty else D0
        current_price = abs(current_notional / signed_qty) if signed_qty else D0
        representative = max(
            rows,
            key=lambda row: abs(
                _dec(getattr(row, "signed_qty", 0))
                * _dec(getattr(row, "current_price", 0))
            ),
        )
        merged = replace(
            representative,
            symbol=key,
            signed_qty=signed_qty,
            avg_price=avg_price,
            current_price=current_price,
            holding_sec=min(
                (_dec(getattr(row, "holding_sec", 0)) for row in rows),
                default=D0,
            ),
            unrealised_pnl=sum(
                (_dec(getattr(row, "unrealised_pnl", 0)) for row in rows),
                D0,
            ),
        )
        aggregated.append(merged)
        brokers = sorted(
            {
                str(getattr(row, "broker", "") or "").lower()
                for row in rows
                if str(getattr(row, "broker", "") or "").strip()
            }
        )
        if len(rows) > 1:
            duplicates.append(
                {
                    "economic_symbol": key,
                    "rows": len(rows),
                    "brokers": brokers,
                    "gross_notional": str(
                        sum(
                            (
                                abs(_dec(getattr(row, "signed_qty", 0)))
                                * _dec(getattr(row, "current_price", 0))
                                for row in rows
                            ),
                            D0,
                        )
                    ),
                }
            )
    return aggregated, {
        "raw_rows": sum(len(rows) for rows in grouped.values()),
        "economic_positions": len(aggregated),
        "duplicate_economic_positions": duplicates,
    }


def _asset_bucket(symbol: str, asset_class: str) -> str:
    role = instrument_role(symbol, asset_class=asset_class)
    if role in {InstrumentRole.CASH_EQUIVALENT, InstrumentRole.LIQUIDITY_RESERVE}:
        return "cash"
    factors = economic_factor_loadings(symbol, asset_class)
    if any("bond" in factor or "credit" in factor for factor in factors):
        return "bond"
    value = str(asset_class or "").strip().lower()
    return value if value in {"crypto", "equity", "forex", "bond"} else "other"


def risk_balanced_weights(
    items: Sequence[tuple[str, str, Decimal]],
    *,
    policy: BalancePolicy,
) -> tuple[dict[str, Decimal], dict[str, Any]]:
    """Blend conviction weights with semantic HRP weights."""
    if not items:
        return {}, {"used": False, "reason": "empty"}
    if len(items) == 1 or not policy.enabled:
        total = sum((max(D0, _dec(weight)) for _, _, weight in items), D0) or D1
        return {
            economic_key(symbol): max(D0, _dec(weight)) / total
            for symbol, _, weight in items
        }, {"used": False, "reason": "single_or_disabled"}

    n = len(items)
    cov = np.zeros((n, n), dtype=float)
    for i, (symbol_i, asset_i, _) in enumerate(items):
        bucket_i = _asset_bucket(symbol_i, asset_i)
        vol_i = policy.volatility_by_asset.get(
            bucket_i, policy.volatility_by_asset.get("other", Decimal("0.25"))
        )
        for j, (symbol_j, asset_j, _) in enumerate(items):
            bucket_j = _asset_bucket(symbol_j, asset_j)
            vol_j = policy.volatility_by_asset.get(
                bucket_j, policy.volatility_by_asset.get("other", Decimal("0.25"))
            )
            if i == j:
                corr = D1
            else:
                semantic = factor_overlap(symbol_i, asset_i, symbol_j, asset_j)
                base = (
                    policy.base_correlation_by_asset.get(bucket_i, Decimal("0.10"))
                    if bucket_i == bucket_j
                    else D0
                )
                corr = min(D1, max(base, semantic))
            cov[i, j] = float(vol_i * vol_j * corr)

    hrp = hrp_weights(cov)
    hrp_w = np.asarray(hrp.weights, dtype=float)
    conviction = np.asarray(
        [float(max(Decimal("0.00000001"), _dec(weight))) for _, _, weight in items],
        dtype=float,
    )
    conviction /= conviction.sum()
    blend = float(max(D0, min(D1, policy.hrp_blend)))
    combined = np.power(conviction, 1.0 - blend) * np.power(
        np.maximum(hrp_w, 1e-12), blend
    )
    combined /= combined.sum()
    return {
        economic_key(symbol): Decimal(str(float(combined[index])))
        for index, (symbol, _, _) in enumerate(items)
    }, {
        "used": True,
        "hrp_blend": str(policy.hrp_blend),
        "hrp_fallback": hrp.fallback,
    }


def factor_exposure_from_book(positions: Iterable[Any]) -> dict[str, Decimal]:
    exposure: dict[str, Decimal] = {}
    for position in positions:
        symbol = str(getattr(position, "symbol", "") or "")
        asset_class = str(getattr(position, "asset_class", "") or "")
        notional = _dec(getattr(position, "signed_notional", 0))
        for factor, loading in economic_factor_loadings(symbol, asset_class).items():
            exposure[factor] = exposure.get(factor, D0) + notional * loading
    return exposure


def factor_exposure_from_portfolio_state(
    portfolio_state: Mapping[str, Any],
) -> dict[str, Decimal]:
    exposure: dict[str, Decimal] = {}
    for position_key, raw in (portfolio_state.get("positions") or {}).items():
        row = raw if isinstance(raw, Mapping) else {}
        symbol = str(
            row.get("symbol")
            or str(position_key or "").split(":", 1)[-1]
            or ""
        )
        asset_class = str(row.get("asset_class", "") or "")
        quantity = _dec(row.get("quantity"))
        price = _dec(row.get("current_price") or row.get("avg_entry_price"))
        notional = quantity * price
        for factor, loading in economic_factor_loadings(symbol, asset_class).items():
            exposure[factor] = exposure.get(factor, D0) + notional * loading
    return exposure


def portfolio_admission_efficiency(
    *,
    symbol: str,
    asset_class: str,
    proposed_notional: Decimal,
    expected_edge_bps: Decimal,
    current_factor_exposure: Mapping[str, Decimal],
    nav: Decimal,
    policy: BalancePolicy,
) -> tuple[Decimal, dict[str, Any]]:
    """Score incremental edge after cost and factor crowding."""
    notional = abs(_dec(proposed_notional))
    if notional <= 0 or nav <= 0:
        return D0, {"allow": False, "reason": "invalid_notional_or_nav"}
    loadings = economic_factor_loadings(symbol, asset_class)
    crowding = D0
    projected: dict[str, str] = {}
    for factor, loading in loadings.items():
        existing = abs(_dec(current_factor_exposure.get(factor, 0)))
        projected_value = existing + notional * abs(loading)
        share = projected_value / nav
        crowding = max(crowding, share)
        projected[factor] = str(share)
    edge = _dec(expected_edge_bps)
    cost = policy.estimated_round_trip_cost_bps
    overlap_cost = policy.overlap_penalty * crowding * Decimal("100")
    efficiency = (edge - cost - overlap_cost) / max(Decimal("1"), edge)
    allow = (
        efficiency >= policy.min_efficiency
        and crowding <= policy.max_incremental_factor_share
    )
    return efficiency, {
        "allow": allow,
        "reason": "approved" if allow else "portfolio_overlap_or_efficiency",
        "factor_crowding": str(crowding),
        "projected_factor_shares": projected,
        "expected_edge_bps": str(edge),
        "estimated_round_trip_cost_bps": str(cost),
        "overlap_cost_bps": str(overlap_cost),
    }


def legacy_reconciliation_plan(
    rows: Sequence[Mapping[str, Any]],
    *,
    nav: Decimal,
    policy: BalancePolicy,
) -> list[dict[str, Any]]:
    """Produce gradual reduce-only actions for legacy duplicate venue rows.

    The largest expression is retained.  A duplicate is actionable only when
    its notional is larger than estimated round-trip cost, so dust is reported
    but not churned merely to make a dashboard look tidy.
    """
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(economic_key(row.get("symbol")), []).append(row)
    plan: list[dict[str, Any]] = []
    planned_rows: set[tuple[str, str]] = set()
    for symbol, members in grouped.items():
        active = []
        for row in members:
            quantity = _dec(row.get("quantity"))
            price = _dec(row.get("current_price") or row.get("avg_entry_price"))
            notional = abs(quantity) * price
            if quantity != 0 and notional > 0:
                active.append((notional, row))
        if len(active) <= 1:
            continue
        active.sort(key=lambda item: item[0], reverse=True)
        preferred = str(active[0][1].get("broker", "") or "")
        for notional, row in active[1:]:
            estimated_cost = notional * policy.estimated_round_trip_cost_bps / Decimal(
                "10000"
            )
            estimated_savings = estimated_cost * policy.expected_avoided_round_trips
            if notional <= estimated_cost:
                continue
            plan.append(
                {
                    "kind": "reduce_duplicate_venue",
                    "economic_symbol": symbol,
                    "broker": str(row.get("broker", "") or ""),
                    "preferred_broker": preferred,
                    "quantity": str(abs(_dec(row.get("quantity")))),
                    "notional": str(notional),
                    "reduce_only": True,
                    "estimated_exit_cost": str(estimated_cost),
                    "estimated_avoided_cost": str(estimated_savings),
                    "nav_share": str(notional / nav) if nav > 0 else "0",
                }
            )
            planned_rows.add(
                (str(row.get("broker", "") or ""), economic_key(row.get("symbol")))
            )

    # Pegged assets accidentally admitted as alpha should return to cash.
    for row in rows:
        role = instrument_role(
            row.get("symbol"),
            asset_class=row.get("asset_class", ""),
            metadata=dict(row.get("metadata") or {})
            if isinstance(row.get("metadata"), Mapping)
            else None,
        )
        if role != InstrumentRole.CASH_EQUIVALENT:
            continue
        notional = abs(_dec(row.get("quantity"))) * _dec(
            row.get("current_price") or row.get("avg_entry_price")
        )
        key = (str(row.get("broker", "") or ""), economic_key(row.get("symbol")))
        if notional <= 0 or key in planned_rows:
            continue
        plan.append(
            {
                "kind": "close_non_alpha_cash_equivalent",
                "economic_symbol": key[1],
                "broker": key[0],
                "quantity": str(abs(_dec(row.get("quantity")))),
                "notional": str(notional),
                "reduce_only": True,
                "estimated_exit_cost": str(
                    notional
                    * policy.estimated_round_trip_cost_bps
                    / Decimal("10000")
                ),
                "estimated_avoided_cost": str(
                    notional
                    * policy.estimated_round_trip_cost_bps
                    / Decimal("10000")
                    * policy.expected_avoided_round_trips
                ),
                "nav_share": str(notional / nav) if nav > 0 else "0",
            }
        )
        planned_rows.add(key)

    # Only true substitutes are auto-planned.  Sector ETF/constituent overlap
    # is reported by invariants but left for the allocator's risk weighting;
    # automatically selling a deliberate stock tilt would be overreach.
    substitutable_factors = {"core_bonds", "global_ex_us_equity"}
    substitute_members = {
        "core_bonds": {"AGG", "BND", "IUSB"},
        "global_ex_us_equity": {"EFA", "VXUS"},
    }
    factor_rows: dict[str, dict[str, tuple[Decimal, Mapping[str, Any]]]] = {}
    for row in rows:
        symbol = economic_key(row.get("symbol"))
        notional = abs(_dec(row.get("quantity"))) * _dec(
            row.get("current_price") or row.get("avg_entry_price")
        )
        for factor in economic_factor_loadings(
            symbol, row.get("asset_class", "")
        ):
            if factor not in substitutable_factors:
                continue
            if symbol not in substitute_members.get(factor, set()):
                continue
            by_symbol = factor_rows.setdefault(factor, {})
            current = by_symbol.get(symbol)
            if current is None or notional > current[0]:
                by_symbol[symbol] = (notional, row)
    for factor, members in factor_rows.items():
        if len(members) <= 1:
            continue
        preferred_symbol = max(members.items(), key=lambda item: item[1][0])[0]
        for symbol, (notional, row) in members.items():
            if symbol == preferred_symbol:
                continue
            key = (str(row.get("broker", "") or ""), symbol)
            if key in planned_rows:
                continue
            plan.append(
                {
                    "kind": "reduce_redundant_factor",
                    "factor": factor,
                    "economic_symbol": symbol,
                    "preferred_symbol": preferred_symbol,
                    "broker": key[0],
                    "quantity": str(abs(_dec(row.get("quantity")))),
                    "notional": str(notional),
                    "reduce_only": True,
                    "estimated_exit_cost": str(
                        notional
                        * policy.estimated_round_trip_cost_bps
                        / Decimal("10000")
                    ),
                    "estimated_avoided_cost": str(
                        notional
                        * policy.estimated_round_trip_cost_bps
                        / Decimal("10000")
                        * policy.expected_avoided_round_trips
                    ),
                    "nav_share": str(notional / nav) if nav > 0 else "0",
                }
            )
            planned_rows.add(key)

    # Broader correlated factors retain a configurable number of independent
    # expressions.  The largest existing expressions are kept; smaller legacy
    # satellites are unwound one at a time by the loop.
    broad_factor_rows: dict[
        str, dict[tuple[str, str], tuple[Decimal, Mapping[str, Any]]]
    ] = {}
    for row in rows:
        symbol = economic_key(row.get("symbol"))
        broker = str(row.get("broker", "") or "")
        notional = abs(_dec(row.get("quantity"))) * _dec(
            row.get("current_price") or row.get("avg_entry_price")
        )
        for factor in economic_factor_loadings(
            symbol, row.get("asset_class", "")
        ):
            if factor not in policy.max_legacy_expressions_by_factor:
                continue
            broad_factor_rows.setdefault(factor, {})[(broker, symbol)] = (
                notional,
                row,
            )
    for factor, members in broad_factor_rows.items():
        limit = policy.max_legacy_expressions_by_factor[factor]
        ordered = sorted(
            members.items(), key=lambda item: item[1][0], reverse=True
        )
        if len(ordered) <= limit:
            continue
        retained = [symbol for (_broker, symbol), _ in ordered[:limit]]
        for key, (notional, row) in ordered[limit:]:
            if key in planned_rows:
                continue
            exit_cost = (
                notional
                * policy.estimated_round_trip_cost_bps
                / Decimal("10000")
            )
            plan.append(
                {
                    "kind": "reduce_excess_factor_expression",
                    "factor": factor,
                    "economic_symbol": key[1],
                    "retained_symbols": retained,
                    "broker": key[0],
                    "quantity": str(abs(_dec(row.get("quantity")))),
                    "notional": str(notional),
                    "reduce_only": True,
                    "estimated_exit_cost": str(exit_cost),
                    "estimated_avoided_cost": str(
                        exit_cost * policy.expected_avoided_round_trips
                    ),
                    "nav_share": str(notional / nav) if nav > 0 else "0",
                }
            )
            planned_rows.add(key)
    return plan
