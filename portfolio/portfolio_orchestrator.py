"""
portfolio/portfolio_orchestrator.py
===================================

Portfolio-level netting orchestrator (D156).

PROBLEM (diagnosed 2026-06-06, see DECISIONS D156):
    With 7 directional strategies enabled at once, the book degenerated into
    an accidental market-neutral basket (gross ~103%, net ~14%) of ~45
    equal-weighted names. Strategies took opposite sides of the SAME
    instrument (mean_reversion LONG SPY vs volume_flow SHORT SPY), cancelling
    each other's edge, while the global-edge rotation/recycle layer
    force-closed maturing positions to chase marginally-better opportunities
    (``global_edge_rotation`` closed 10 positions at a 0% win rate). Net
    result: a self-hedged book that captured no market direction and bled
    slowly through round-trip costs.

SOLUTION:
    A single portfolio-construction step between the strategy *alphas* and the
    risk/execution path — exactly how production multi-strategy books work:

        1. ALPHA COMBINATION  — collect every strategy's directional intent
           for a symbol and net them into ONE conviction-weighted view.
           Opposing strategies reduce conviction; they never open an
           offsetting long+short pair.

        2. PORTFOLIO CONSTRUCTION — turn net convictions into target weights:
           conviction-concentrated (not equal-weighted), per-name capped,
           scaled to a mode-dependent gross budget, with deliberate NET
           management (|net| <= net_cap × gross) so the book expresses a
           real directional view instead of collapsing to noise.

        3. EDGE-PROTECTED REBALANCE — diff the target book against the
           current book and emit the MINIMAL set of orders. A position with
           remaining edge is never closed to fund a marginally-better one:
           flips require a strong opposing conviction, young/profitable
           positions are protected, and sub-band diffs are suppressed
           (kills the 71-min churn).

This module is a PURE function over plain dataclasses — no DB, no I/O, no
globals — so it is fully unit-testable and its decisions are auditable. All
money/quantities are ``Decimal`` (rule 3). It NEVER places orders or bypasses
risk (rule 2): it emits *intents* that the caller routes through the existing
RiskEngine → ExecutionEngine path unchanged.

Gated OFF by default (``config/strategies.yaml::portfolio_orchestrator.enabled``).
When disabled the caller keeps its existing behaviour exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from core.instrument_semantics import canonical_economic_symbol
from portfolio.balance import BalancePolicy, aggregate_book_positions, risk_balanced_weights
from portfolio.cluster_map import theme_for, theme_sign_if_bought

D0 = Decimal("0")
D1 = Decimal("1")


def _dec(x: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(x))
    except Exception:  # noqa: BLE001
        return Decimal(default)


def _sign_of_side(side: str) -> int:
    s = (side or "").strip().lower()
    if s in ("buy", "long", "b"):
        return 1
    if s in ("sell", "short", "s"):
        return -1
    return 0


def _symbol_key(symbol: Any) -> str:
    """Canonical key for portfolio netting.

    Market-data symbols can differ from broker-native symbols. In particular,
    yfinance-style FX pairs arrive as ``GBPUSD=X`` while IBKR/local positions
    are held as ``GBPUSD``. Those must net as the same book slot, otherwise the
    allocator repeatedly tries to open a duplicate position that final risk
    rejects as already capped.
    """
    s = canonical_economic_symbol(symbol)
    if s.endswith("=X"):
        return s[:-2]
    return s


# ─────────────────────────────────────────────────────────────────────────────
# Inputs
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class StrategyIntent:
    """One strategy's directional view on one symbol (an *alpha*, not an order)."""

    symbol: str
    side: str                      # "buy" / "sell"
    conviction: Decimal            # 0..1 (typically the strategy confidence)
    strategy: str
    asset_class: str = ""
    broker: str = ""
    cluster: str | None = None     # optional correlated-group id (e.g. "us_equity_index")

    @property
    def signed_conviction(self) -> Decimal:
        return Decimal(_sign_of_side(self.side)) * _dec(self.conviction)


@dataclass(frozen=True)
class BookPosition:
    """An existing position from the live book / PositionLog."""

    symbol: str
    signed_qty: Decimal
    avg_price: Decimal
    current_price: Decimal
    asset_class: str = ""
    broker: str = ""
    holding_sec: Decimal = D0
    unrealised_pnl: Decimal = D0
    # Optional richer edge estimate (from the global-edge coordinator). When
    # provided it supersedes the unrealised/age proxy in edge protection.
    expected_remaining_edge: Decimal | None = None

    @property
    def signed_notional(self) -> Decimal:
        return self.signed_qty * self.current_price

    @property
    def direction(self) -> int:
        if self.signed_qty > 0:
            return 1
        if self.signed_qty < 0:
            return -1
        return 0


@dataclass(frozen=True)
class OrchestratorConfig:
    enabled: bool = False
    entry_conviction_threshold: Decimal = Decimal("0.15")
    flip_conviction_threshold: Decimal = Decimal("0.45")
    hard_flip_conviction: Decimal = Decimal("0.75")
    concentration_exponent: Decimal = Decimal("1.5")
    # D163 — the per-name cap is the safety CEILING (max concentration in one
    # name); the operating cap is derived per-tick from live opportunity
    # breadth (``gross / number_of_firing_edges``, clamped to
    # [min_position_pct_of_nav, max_position_pct_of_nav]). So with many proven
    # edges the book diversifies, with few it concentrates — never a flat
    # constant, always bounded by these rails.
    max_position_pct_of_nav: Decimal = Decimal("0.08")
    min_position_pct_of_nav: Decimal = Decimal("0.02")
    gross_target_pct: dict[str, Decimal] = field(
        default_factory=lambda: {
            "defender": Decimal("0.50"),
            "trader": Decimal("0.90"),
            "hunter": Decimal("1.30"),
        }
    )
    net_cap_pct_of_gross: Decimal = Decimal("0.60")
    rebalance_band_pct_of_nav: Decimal = Decimal("0.01")
    min_hold_sec_before_flip: Decimal = Decimal("1800")
    # A strategy not emitting on the current bar is not evidence that its
    # thesis reversed.  Keep silence distinct from an explicit opposing
    # signal; dedicated stop/risk/session/reconciliation paths remain able to
    # reduce or close positions independently of this allocator.
    close_on_signal_silence: bool = False
    # D160 — cluster-aware construction. When true, correlated same-direction
    # signals (forex by USD direction, equity-index by beta, crypto by
    # crypto-beta) are recognised as ONE theme and expressed as a single big
    # position in the strongest member, instead of fragmenting capital +
    # conviction across many near-identical names. This is awareness, NOT a cap
    # (the per-name and cluster CAPS stay removed — D159).
    cluster_consolidation: bool = False
    cluster_conviction_cap: Decimal = Decimal("1.5")  # ceiling on summed theme conviction
    cluster_same_strategy_bonus: Decimal = Decimal("0.10")
    cluster_multi_strategy_bonus: Decimal = Decimal("0.25")
    # D162 — never average down. The backtest that certified every weapon
    # holds ONE entry per signal; live, mean-reversion's conviction RISES as
    # price falls (deeper oversold = stronger signal), so without this guard
    # the orchestrator sizes UP into a falling position (observed: GLD topped
    # up to ~69% NAV through a −3.8% slide). Increases are only allowed while
    # the position is at or above water; underwater positions may be held,
    # reduced, closed or flipped — never added to.
    no_average_down: bool = True
    # A position is "still has edge" (→ protected from a marginal flip/close)
    # when its expected remaining edge (or unrealised P&L proxy) exceeds this.
    close_edge_floor: Decimal = D0
    # Per-strategy trust weights (recent-performance scaling lives in the
    # caller; this is the resolved multiplier per strategy name).
    strategy_trust: dict[str, Decimal] = field(default_factory=dict)
    min_trust: Decimal = Decimal("0.25")
    max_trust: Decimal = Decimal("1.50")
    # D166 (Phase 2) — scoreboard gates size increases. The live P&L posterior
    # is only trusted once a weapon has at least this many live CLOSES (one
    # noisy fill must not flip trust). Below it, trust = the backtest prior
    # only. Once enough live closes exist AND the weapon is net-negative, its
    # trust is capped at neutral (1.0) regardless of an optimistic backtest
    # prior — never amplify a proven-live loser.
    min_live_closes_for_posterior: int = 8
    # Portfolio balance is a separate axis from conviction.  Conviction
    # chooses what deserves capital; semantic HRP determines how much risk
    # each correlated expression receives.
    balance_policy: BalancePolicy = field(default_factory=BalancePolicy)
    # ── D158 Phase 2 — heterogeneous hunter army ──────────────────────────
    # Each weapon has a TEMPERAMENT (sniper / shotgun / knife) — an intrinsic
    # style independent of the market — and the global MODE (defender/trader/
    # hunter) is a systemic THREAT throttle that scales the whole army. A
    # weapon's effective aggression = size_mult × (1 − threat × defensive_cut),
    # so in calm markets the army is heterogeneous (each weapon by its style)
    # and in danger everyone retreats together but the eager weapons (knives)
    # are cut hardest while resilient snipers hold. Empty config → all factors
    # 1.0 (no change; backward compatible).
    temperaments: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    weapon_temperament: dict[str, str] = field(default_factory=dict)
    mode_threat: dict[str, Decimal] = field(
        default_factory=lambda: {
            "hunter": Decimal("0.0"),
            "trader": Decimal("0.35"),
            "defender": Decimal("0.75"),
        }
    )

    @classmethod
    def from_yaml(cls, raw: dict[str, Any] | None) -> "OrchestratorConfig":
        raw = raw or {}
        gt_raw = raw.get("gross_target_pct") or {}
        gross = {
            str(k).strip().lower(): _dec(v)
            for k, v in gt_raw.items()
        } or None
        trust_raw = raw.get("strategy_trust") or {}
        # ``strategy_trust`` in YAML is a policy block; resolved per-strategy
        # multipliers are injected by the caller, so only carry static
        # overrides if present as a flat name→number map.
        trust = {
            str(k).strip(): _dec(v)
            for k, v in trust_raw.items()
            if isinstance(v, (int, float, str))
        }
        kwargs: dict[str, Any] = {
            "enabled": bool(raw.get("enabled", False)),
        }
        for key in (
            "entry_conviction_threshold",
            "flip_conviction_threshold",
            "hard_flip_conviction",
            "concentration_exponent",
            "max_position_pct_of_nav",
            "min_position_pct_of_nav",
            "net_cap_pct_of_gross",
            "rebalance_band_pct_of_nav",
            "min_hold_sec_before_flip",
            "close_edge_floor",
            "min_trust",
            "max_trust",
            "cluster_conviction_cap",
            "cluster_same_strategy_bonus",
            "cluster_multi_strategy_bonus",
        ):
            if raw.get(key) is not None:
                kwargs[key] = _dec(raw.get(key))
        if raw.get("min_live_closes_for_posterior") is not None:
            try:
                kwargs["min_live_closes_for_posterior"] = int(raw.get("min_live_closes_for_posterior"))
            except (TypeError, ValueError):
                pass
        if raw.get("cluster_consolidation") is not None:
            kwargs["cluster_consolidation"] = bool(raw.get("cluster_consolidation"))
        if raw.get("no_average_down") is not None:
            kwargs["no_average_down"] = bool(raw.get("no_average_down"))
        if raw.get("close_on_signal_silence") is not None:
            kwargs["close_on_signal_silence"] = bool(raw.get("close_on_signal_silence"))
        kwargs["balance_policy"] = BalancePolicy.from_mapping(
            raw.get("risk_balance") if isinstance(raw.get("risk_balance"), dict) else {}
        )
        if gross is not None:
            kwargs["gross_target_pct"] = gross
        if trust:
            kwargs["strategy_trust"] = trust
        # D158 Phase 2 — temperament + threat config.
        temps_raw = raw.get("temperaments") or {}
        if isinstance(temps_raw, dict) and temps_raw:
            temps: dict[str, dict[str, Decimal]] = {}
            for tname, tvals in temps_raw.items():
                if isinstance(tvals, dict):
                    temps[str(tname).strip().lower()] = {
                        "size_mult": _dec(tvals.get("size_mult", 1), "1"),
                        "defensive_cut": _dec(tvals.get("defensive_cut", 0), "0"),
                    }
            if temps:
                kwargs["temperaments"] = temps
        wt_raw = raw.get("weapon_temperament") or {}
        if isinstance(wt_raw, dict) and wt_raw:
            kwargs["weapon_temperament"] = {
                str(k).strip(): str(v).strip().lower() for k, v in wt_raw.items()
            }
        mt_raw = raw.get("mode_threat") or {}
        if isinstance(mt_raw, dict) and mt_raw:
            kwargs["mode_threat"] = {
                str(k).strip().lower(): _dec(v) for k, v in mt_raw.items()
            }
        return cls(**kwargs)

    def trust_for(self, strategy: str) -> Decimal:
        t = self.strategy_trust.get(strategy)
        if t is None:
            return D1
        return max(self.min_trust, min(self.max_trust, t))

    def gross_target_for(self, mode: str) -> Decimal:
        return self.gross_target_pct.get((mode or "trader").strip().lower(), Decimal("0.90"))

    # ── D158 Phase 2 helpers ──────────────────────────────────────────────
    def threat_for(self, mode: str) -> Decimal:
        """Systemic threat level [0,1] for the global mode (hunter≈0, defender≈high)."""
        t = self.mode_threat.get((mode or "trader").strip().lower())
        if t is None:
            return Decimal("0.35")
        return max(D0, min(D1, t))

    def temperament_factor(self, strategy: str, threat: Decimal) -> Decimal:
        """Per-weapon aggression multiplier = size_mult × (1 − threat × defensive_cut).

        ``size_mult`` is the weapon's intrinsic style (sniper sizes bigger per
        shot, knife smaller); ``defensive_cut`` is how hard systemic threat
        throttles it. Returns 1.0 when no temperament is configured for the
        strategy (backward compatible). Never returns < 0.
        """
        tname = self.weapon_temperament.get(strategy)
        if tname is None:
            return D1
        spec = self.temperaments.get(tname)
        if not spec:
            return D1
        size_mult = spec.get("size_mult", D1)
        defensive_cut = spec.get("defensive_cut", D0)
        factor = size_mult * (D1 - threat * defensive_cut)
        return factor if factor > D0 else D0


# ─────────────────────────────────────────────────────────────────────────────
# Outputs
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class TargetPosition:
    symbol: str
    target_notional: Decimal          # signed (+long / -short)
    net_conviction: Decimal           # signed combined alpha
    contributing: list[str]           # strategy names that fed this target
    had_conflict: bool                # strategies disagreed on direction
    asset_class: str = ""
    broker: str = ""


@dataclass
class OrchestratedOrder:
    symbol: str
    side: str                         # "buy" / "sell"
    delta_notional: Decimal           # absolute notional to trade
    reduce_only: bool
    close_only: bool
    reason: str
    net_conviction: Decimal
    contributing: list[str]
    target_notional: Decimal          # signed absolute portfolio target
    asset_class: str = ""
    broker: str = ""


@dataclass
class OrchestrationResult:
    orders: list[OrchestratedOrder]
    targets: list[TargetPosition]
    diagnostics: dict[str, Any]


def consolidate_clusters(
    intents: list[StrategyIntent], config: OrchestratorConfig
) -> tuple[list[StrategyIntent], int]:
    """Collapse correlated same-theme signals into one big bet (D160).

    Forex pairs sharing a USD direction, equity-index ETFs sharing beta, and
    crypto pairs sharing crypto-beta are recognised as ONE theme. For each
    multi-member theme this nets the signed conviction and emits a single
    StrategyIntent on the strongest member. Correlated signals from one weapon
    are treated as one piece of evidence (max conviction + a small breadth
    bonus); larger cluster conviction is reserved for genuinely diverse
    weapon confirmation.
    Non-clustered symbols (and single-member clusters) pass through unchanged.

    Returns ``(new_intents, clusters_consolidated)``.
    """
    grouped: dict[str, list[tuple[StrategyIntent, int]]] = {}
    passthrough: list[StrategyIntent] = []
    for it in intents:
        cluster, sign = theme_for(it.symbol, it.asset_class, it.side)
        if cluster is None or sign == 0:
            passthrough.append(it)
        else:
            grouped.setdefault(cluster, []).append((it, sign))

    out: list[StrategyIntent] = list(passthrough)
    consolidated = 0
    for cluster, members in grouped.items():
        if len(members) <= 1:
            out.append(members[0][0])
            continue
        evidence_by_strategy: dict[str, dict[str, Decimal]] = {}
        for it, sign in members:
            signed = Decimal(sign) * _dec(it.conviction)
            slot = evidence_by_strategy.setdefault(it.strategy, {"pos": D0, "neg": D0})
            if signed > 0:
                slot["pos"] = max(slot["pos"], signed)
            elif signed < 0:
                slot["neg"] = min(slot["neg"], signed)

        by_strategy: dict[str, Decimal] = {
            strategy: vals["pos"] + vals["neg"]
            for strategy, vals in evidence_by_strategy.items()
            if vals["pos"] + vals["neg"] != 0
        }

        # Net independent strategy evidence in the cluster's reference
        # direction. Multiple correlated symbols from the same strategy do not
        # become multiple confirmations.
        net = sum(by_strategy.values(), D0)
        if net == 0:
            consolidated += 1  # signals cancel → express nothing for this theme
            continue
        # Express via the single strongest member (most conviction).
        expr_it, _ = max(members, key=lambda m: _dec(m[0].conviction))
        net_dir = 1 if net > 0 else -1
        base = theme_sign_if_bought(expr_it.symbol, expr_it.asset_class)
        side = "buy" if (base == net_dir) else "sell"
        aligned_strategies = [
            strat for strat, val in by_strategy.items()
            if (val > 0 and net > 0) or (val < 0 and net < 0)
        ]
        aligned_members = [
            it for it, sign in members
            if (Decimal(sign) * _dec(it.conviction) > 0 and net > 0)
            or (Decimal(sign) * _dec(it.conviction) < 0 and net < 0)
        ]
        breadth_bonus = D0
        if len(aligned_strategies) > 1:
            breadth_bonus = config.cluster_multi_strategy_bonus * Decimal(len(aligned_strategies) - 1)
        elif len(aligned_members) > 1:
            breadth_bonus = config.cluster_same_strategy_bonus
        cap = config.cluster_conviction_cap if config.cluster_conviction_cap > 0 else Decimal("1.5")
        conviction = min(abs(net) + breadth_bonus, cap)
        out.append(
            StrategyIntent(
                symbol=expr_it.symbol,
                side=side,
                conviction=conviction,
                strategy=expr_it.strategy,  # keep trust/temperament resolvable
                asset_class=expr_it.asset_class,
                broker=expr_it.broker,
                cluster=cluster,
            )
        )
        consolidated += 1
    return out, consolidated


# ─────────────────────────────────────────────────────────────────────────────
# Core
# ─────────────────────────────────────────────────────────────────────────────
def orchestrate(
    intents: Iterable[StrategyIntent],
    book: Iterable[BookPosition],
    *,
    nav: Decimal,
    mode: str,
    config: OrchestratorConfig,
) -> OrchestrationResult:
    """Combine strategy alphas + current book into an edge-protected order set.

    Returns an :class:`OrchestrationResult` whose ``orders`` are minimal diffs
    to move the book toward the conviction-weighted target. Pure & deterministic.
    """
    nav = _dec(nav)
    intents = list(intents)
    raw_intent_count = len(intents)
    # D160 — cluster-aware construction: collapse correlated same-direction
    # signals (e.g. 5 short-USD forex pairs) into ONE big bet on the strongest
    # member, before per-symbol netting. Awareness, not a cap.
    clusters_consolidated = 0
    if config.cluster_consolidation:
        intents, clusters_consolidated = consolidate_clusters(intents, config)
    raw_book_list = list(book)
    book_list, book_diag = aggregate_book_positions(raw_book_list)
    book_by_sym: dict[str, BookPosition] = {}
    symbol_aliases_normalized = 0
    for p in book_list:
        key = _symbol_key(p.symbol)
        if key != str(p.symbol):
            symbol_aliases_normalized += 1
        book_by_sym[key] = p

    diag: dict[str, Any] = {
        "intent_count": len(intents),
        "raw_intent_count": raw_intent_count,
        "clusters_consolidated": clusters_consolidated,
        "book_positions": len(book_list),
        "raw_book_rows": len(raw_book_list),
        "duplicate_economic_positions": book_diag.get(
            "duplicate_economic_positions", []
        ),
        "netted_symbols": 0,
        "conflicts_resolved": 0,
        "protected_positions": 0,
        "suppressed_rebalances": 0,
        "flips": 0,
        "closes": 0,
        "opens": 0,
        "increases": 0,
        "reduces": 0,
    }

    if nav <= 0:
        diag["abort"] = "nav<=0"
        return OrchestrationResult([], [], diag)

    # D158 Phase 2 — systemic threat from the global mode, and the per-weapon
    # temperament factor applied below. Calm (hunter) → threat≈0 → weapons keep
    # their intrinsic style; danger (defender) → threat high → eager weapons
    # cut hardest, resilient snipers least.
    threat = config.threat_for(mode)
    temperament_applied: dict[str, str] = {}

    # ── 1. ALPHA COMBINATION — net intents per symbol ──────────────────────
    combined: dict[str, dict[str, Any]] = {}
    for it in intents:
        sym = _symbol_key(it.symbol)
        if sym != str(it.symbol):
            symbol_aliases_normalized += 1
        temp_factor = config.temperament_factor(it.strategy, threat)
        if it.strategy in config.weapon_temperament:
            temperament_applied[it.strategy] = (
                f"{config.weapon_temperament[it.strategy]}×{temp_factor:.3f}"
            )
        sc = it.signed_conviction * config.trust_for(it.strategy) * temp_factor
        slot = combined.setdefault(
            sym,
            {
                "net": D0,
                "contrib": [],
                "pos_dir": False,
                "neg_dir": False,
                "asset_class": it.asset_class,
                "broker": it.broker,
            },
        )
        slot["net"] += sc
        slot["contrib"].append(it.strategy)
        if sc > 0:
            slot["pos_dir"] = True
        elif sc < 0:
            slot["neg_dir"] = True
        if not slot["asset_class"] and it.asset_class:
            slot["asset_class"] = it.asset_class
        if not slot["broker"] and it.broker:
            slot["broker"] = it.broker

    for sym, slot in combined.items():
        if len(slot["contrib"]) > 1:
            diag["netted_symbols"] += 1
        if slot["pos_dir"] and slot["neg_dir"]:
            diag["conflicts_resolved"] += 1

    # ── 2. PORTFOLIO CONSTRUCTION — convictions → target weights ───────────
    # Candidate universe: any symbol with a meaningful net conviction OR an
    # existing position (so the book can be reduced/closed deliberately).
    entry_thr = config.entry_conviction_threshold
    candidates: dict[str, Decimal] = {}  # symbol -> signed net conviction
    for sym, slot in combined.items():
        net = slot["net"]
        if abs(net) >= entry_thr or sym in book_by_sym:
            candidates[sym] = net
    for sym in book_by_sym:
        candidates.setdefault(sym, D0)  # held but no fresh signal

    # Raw concentration weight from |conviction| ^ exponent. Symbols with no
    # conviction (held-only) get zero target weight → they become close
    # candidates unless edge-protected below.
    exp = config.concentration_exponent
    raw_w: dict[str, Decimal] = {}
    for sym, net in candidates.items():
        mag = abs(net)
        raw_w[sym] = (mag ** exp) if mag > 0 else D0
    total_w = sum(raw_w.values())

    gross_frac = config.gross_target_for(mode)
    gross_budget = nav * gross_frac
    # D163 — dynamic per-name cap from live opportunity breadth. The operating
    # cap is ``gross / number_of_firing_edges`` (so the more independent proven
    # edges fire, the more the book diversifies; the fewer, the more it
    # concentrates into the available edge), clamped to the configured safety
    # rails. This replaces the flat ``max_position_pct_of_nav`` operating point
    # with a signal-derived one bounded by that ceiling and the floor.
    firing_breadth = sum(1 for net in candidates.values() if abs(net) >= entry_thr)
    breadth_div = Decimal(firing_breadth) if firing_breadth > 0 else D1
    floor_frac = config.min_position_pct_of_nav
    ceil_frac = config.max_position_pct_of_nav
    if floor_frac > ceil_frac:
        floor_frac = ceil_frac
    per_name_frac = gross_frac / breadth_div
    if per_name_frac > ceil_frac:
        per_name_frac = ceil_frac
    elif per_name_frac < floor_frac:
        per_name_frac = floor_frac
    per_name_cap = nav * per_name_frac

    targets: dict[str, TargetPosition] = {}
    for sym, net in candidates.items():
        slot = combined.get(sym, {})
        direction = 1 if net > 0 else (-1 if net < 0 else 0)
        if total_w > 0 and raw_w[sym] > 0:
            weight = raw_w[sym] / total_w
            notional = gross_budget * weight
            if notional > per_name_cap:
                notional = per_name_cap
        else:
            notional = D0
        target_notional = Decimal(direction) * notional
        targets[sym] = TargetPosition(
            symbol=sym,
            target_notional=target_notional,
            net_conviction=net,
            contributing=list(slot.get("contrib", [])),
            had_conflict=bool(slot.get("pos_dir") and slot.get("neg_dir")),
            asset_class=str(slot.get("asset_class", "") or (book_by_sym.get(sym).asset_class if sym in book_by_sym else "")),
            broker=str(slot.get("broker", "") or (book_by_sym.get(sym).broker if sym in book_by_sym else "")),
        )

    # Risk-aware construction.  A large number of correlated tickers must not
    # masquerade as diversification: semantic ETF/theme overlap supplies a
    # stable covariance prior and HRP distributes the gross budget by marginal
    # risk.  Conviction remains in the blend, so stronger alpha still receives
    # more capital without allowing crypto/sector breadth to dominate risk.
    active_target_items = [
        (sym, target.asset_class, raw_w.get(sym, D0))
        for sym, target in targets.items()
        if target.target_notional != 0 and raw_w.get(sym, D0) > 0
    ]
    balanced_weights, balance_diag = risk_balanced_weights(
        active_target_items,
        policy=config.balance_policy,
    )
    if balance_diag.get("used"):
        for sym, target in targets.items():
            balanced_weight = balanced_weights.get(sym)
            if balanced_weight is None or target.target_notional == 0:
                continue
            direction = D1 if target.target_notional > 0 else -D1
            target.target_notional = direction * min(
                per_name_cap,
                gross_budget * balanced_weight,
            )
    diag["risk_balance"] = balance_diag

    # ── 2b. DELIBERATE NET MANAGEMENT — cap |net| at net_cap × gross ───────
    # Only meaningful when BOTH sides exist: trimming the heavier side reduces
    # |net| while the lighter side holds gross up, lowering the net/gross
    # ratio. For a one-sided book net == gross by definition (fully
    # directional, which is the *desired* conviction expression here), so the
    # cap is a no-op — we do not shrink a high-conviction one-way book.
    long_sum = sum(t.target_notional for t in targets.values() if t.target_notional > 0)
    short_sum = sum(-t.target_notional for t in targets.values() if t.target_notional < 0)
    gross_t = long_sum + short_sum
    net_t = long_sum - short_sum
    if long_sum > 0 and short_sum > 0 and gross_t > 0:
        if abs(net_t) > config.net_cap_pct_of_gross * gross_t:
            heavy_sign = 1 if net_t > 0 else -1
            heavy = [t for t in targets.values()
                     if (1 if t.target_notional > 0 else -1) == heavy_sign and t.target_notional != 0]
            heavy_sum = sum(abs(t.target_notional) for t in heavy)
            # Trim Δ off the heavy side so (net-Δ) = net_cap·(gross-Δ):
            #   Δ = (|net| - net_cap·gross) / (1 - net_cap)
            denom = (D1 - config.net_cap_pct_of_gross)
            if heavy_sum > 0 and denom > 0:
                delta = (abs(net_t) - config.net_cap_pct_of_gross * gross_t) / denom
                delta = min(delta, heavy_sum)
                scale = max(D0, (heavy_sum - delta) / heavy_sum)
                for t in heavy:
                    t.target_notional = t.target_notional * scale
                diag["net_capped"] = True

    # ── 3. EDGE-PROTECTED REBALANCE — diff target vs book ──────────────────
    band = nav * config.rebalance_band_pct_of_nav
    orders: list[OrchestratedOrder] = []

    for sym, tgt in targets.items():
        pos = book_by_sym.get(sym)
        cur_notional = pos.signed_notional if pos else D0
        desired = tgt.target_notional
        cur_dir = (1 if cur_notional > 0 else (-1 if cur_notional < 0 else 0))
        des_dir = (1 if desired > 0 else (-1 if desired < 0 else 0))

        # Edge protection: should we be allowed to reduce/flip this position?
        if pos is not None and cur_dir != 0:
            opposing = (des_dir != 0 and des_dir != cur_dir)
            wants_flat = (des_dir == 0)
            if opposing or wants_flat:
                conv_against = abs(tgt.net_conviction)
                has_edge = _position_has_edge(pos, config)
                young = pos.holding_sec < config.min_hold_sec_before_flip
                # Protect a position with remaining edge unless the opposing
                # conviction clears the (hard) flip bar. This is the direct
                # fix for the 0%-win rotation bleed.
                flip_bar = config.hard_flip_conviction if (young or has_edge) else config.flip_conviction_threshold
                if opposing and conv_against < flip_bar:
                    diag["protected_positions"] += 1
                    continue
                # No target means the weapon is silent, not that it supplied
                # exit evidence.  By default, never manufacture a close from
                # that absence. Explicit opposing signals still use the flip
                # rules above, while stops, risk derisk, session exits,
                # reconciliation and evidence-backed recycling are separate
                # paths and retain full authority.
                if wants_flat and not config.close_on_signal_silence:
                    diag["protected_positions"] += 1
                    diag["silence_closes_suppressed"] = (
                        int(diag.get("silence_closes_suppressed", 0)) + 1
                    )
                    continue
                # Legacy opt-in: when silence-close is explicitly enabled,
                # retain the existing edge and minimum-hold protections.
                if wants_flat and (has_edge or young):
                    diag["protected_positions"] += 1
                    continue

        # Flip handling: cross-zero in two steps for safety — this tick only
        # closes to flat (reduce_only); re-entry happens next tick if the
        # opposing conviction persists. Avoids accidental over-shoot.
        if pos is not None and cur_dir != 0 and des_dir != 0 and des_dir != cur_dir:
            delta = abs(cur_notional)
            orders.append(
                OrchestratedOrder(
                    symbol=sym, side=("sell" if cur_dir > 0 else "buy"),
                    delta_notional=delta, reduce_only=True, close_only=True,
                    reason="flip_close_to_flat",
                    net_conviction=tgt.net_conviction, contributing=tgt.contributing,
                    target_notional=tgt.target_notional,
                    asset_class=tgt.asset_class, broker=tgt.broker,
                )
            )
            diag["flips"] += 1
            continue

        delta_signed = desired - cur_notional
        close_only = (des_dir == 0 and cur_dir != 0)
        # The rebalance band suppresses small *adjustments* to avoid churn,
        # but a deliberate close-to-flat (the position is unwanted and not
        # edge-protected) must always proceed regardless of its size.
        if not close_only and abs(delta_signed) < band:
            if pos is not None:
                diag["suppressed_rebalances"] += 1
            continue

        # D162 — never average down: an INCREASE of an underwater position is
        # suppressed (mean-reversion's conviction rises as price falls, so
        # without this the book sizes up into the loss — the GLD slide).
        is_increase = (cur_dir != 0 and des_dir == cur_dir and abs(desired) > abs(cur_notional))
        if (
            config.no_average_down
            and is_increase
            and pos is not None
            and pos.unrealised_pnl < 0
        ):
            diag["averaging_down_blocked"] = diag.get("averaging_down_blocked", 0) + 1
            continue

        is_reduction = (cur_dir != 0 and des_dir == cur_dir and abs(desired) < abs(cur_notional)) or close_only
        # A same-direction target wobble is not a new thesis. Do not pay an
        # exit fee and then re-enter a young position when cross-sectional
        # weights move between otherwise identical feature bars. Dedicated
        # risk exits remain outside this pure allocator and are unaffected.
        if (
            is_reduction
            and not close_only
            and pos is not None
            and pos.holding_sec < config.min_hold_sec_before_flip
        ):
            diag["protected_positions"] += 1
            diag["young_reductions_suppressed"] = (
                int(diag.get("young_reductions_suppressed", 0)) + 1
            )
            continue
        side = "buy" if delta_signed > 0 else "sell"
        orders.append(
            OrchestratedOrder(
                symbol=sym, side=side, delta_notional=abs(delta_signed),
                reduce_only=bool(is_reduction), close_only=bool(close_only),
                reason=_order_reason(cur_dir, des_dir, abs(desired), abs(cur_notional)),
                net_conviction=tgt.net_conviction, contributing=tgt.contributing,
                target_notional=tgt.target_notional,
                asset_class=tgt.asset_class, broker=tgt.broker,
            )
        )
        if close_only:
            diag["closes"] += 1
        elif cur_dir == 0:
            diag["opens"] += 1
        elif is_reduction:
            diag["reduces"] += 1
        else:
            diag["increases"] += 1

    diag["gross_target"] = str(gross_t)
    diag["net_target"] = str(net_t)
    diag["gross_budget"] = str(gross_budget)
    # D163 — signal-derived sizing surface (so the dynamic per-name cap and the
    # edge-Kelly trust are auditable, not hidden constants).
    diag["firing_breadth"] = firing_breadth
    diag["per_name_frac"] = str(per_name_frac)
    diag["per_name_cap"] = str(per_name_cap)
    diag["strategy_trust"] = {k: str(v) for k, v in config.strategy_trust.items()}
    diag["symbol_aliases_normalized"] = symbol_aliases_normalized
    # D158 Phase 2 — surface the army's posture so the operator can SEE the
    # heterogeneous landscape (global threat + per-weapon effective factor).
    diag["mode"] = str(mode)
    diag["threat_level"] = str(threat)
    diag["temperament_factors"] = temperament_applied
    return OrchestrationResult(orders=orders, targets=list(targets.values()), diagnostics=diag)


def _position_has_edge(pos: BookPosition, config: OrchestratorConfig) -> bool:
    """A position still 'has edge' if its expected remaining edge (or, absent
    that, its unrealised P&L) exceeds the close-edge floor. Profitable or
    coordinator-flagged-positive positions are protected from marginal flips."""
    if pos.expected_remaining_edge is not None:
        return pos.expected_remaining_edge > config.close_edge_floor
    return pos.unrealised_pnl > config.close_edge_floor


def _order_reason(cur_dir: int, des_dir: int, des_mag: Decimal, cur_mag: Decimal) -> str:
    if cur_dir == 0:
        return "open"
    if des_dir == 0:
        return "close"
    if des_mag > cur_mag:
        return "increase"
    return "reduce"


def build_intents_from_candidates(candidates: Iterable[Any]) -> list[StrategyIntent]:
    """Adapter: map ``core.models_runtime.SignalCandidate`` → StrategyIntent.

    SignalCandidate uses ``strategy_name`` and a ``Side`` literal
    ("long"/"short"); ``confidence`` is already a ``Decimal``. Skips rows
    whose side does not resolve to a direction.
    """
    out: list[StrategyIntent] = []
    for c in candidates:
        side = str(getattr(c, "side", "") or "")
        if _sign_of_side(side) == 0:
            continue
        md = getattr(c, "metadata", None) or {}
        cluster = md.get("cluster") if isinstance(md, dict) else None
        out.append(
            StrategyIntent(
                symbol=_symbol_key(getattr(c, "symbol", "")),
                side=side,
                conviction=_dec(getattr(c, "confidence", 0)),
                strategy=str(getattr(c, "strategy_name", "") or ""),
                asset_class=str(getattr(c, "asset_class", "") or ""),
                broker=str((md.get("broker") if isinstance(md, dict) else "") or ""),
                cluster=cluster,
            )
        )
    return out


def build_intents_from_raw_signals(raw_signals: Iterable[Any]) -> list[StrategyIntent]:
    """Adapter: map a list of ``signals.engine.RawSignal`` to StrategyIntent.

    Kept here (not in the loop) so the mapping is unit-testable and the loop
    wiring stays a thin call. Skips ``hold`` signals.
    """
    out: list[StrategyIntent] = []
    for rs in raw_signals:
        side = getattr(rs, "side", "")
        if _sign_of_side(side) == 0:
            continue
        md = getattr(rs, "metadata", None) or {}
        cluster = md.get("cluster") if isinstance(md, dict) else None
        out.append(
            StrategyIntent(
                symbol=_symbol_key(getattr(rs, "symbol", "")),
                side=side,
                conviction=_dec(getattr(rs, "confidence", 0)),
                strategy=str(getattr(rs, "strategy", "")),
                asset_class=str(getattr(rs, "asset_class", "") or ""),
                broker=str(getattr(rs, "broker", "") or ""),
                cluster=cluster,
            )
        )
    return out
