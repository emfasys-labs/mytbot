"""
execution/router.py
====================
Smart Order Router (SOR).

Given a signal, decides which broker gives the best execution:
- Is the asset available on this broker?
- What's the current spread?
- What are the fees?
- Is the broker currently healthy?

The router returns the optimal broker name.
The execution engine then routes the order there.

Initially simple — just checks availability.
Later: adds real-time spread comparison across brokers.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
import math
from typing import Optional

from brokers.permissions import get_permissions

logger = logging.getLogger(__name__)

# Which assets each broker can trade
# This gets extended as brokers are added
BROKER_ASSET_MAP = {
    "ibkr": {
        "equity", "etf", "bond", "forex", "option", "future", "crypto"
    },
    "kraken": {
        "crypto"
    },
    "binance": {
        "crypto"
    },
    "bybit": {
        "crypto",
        "future",
    },
    "alpaca": {
        "equity", "etf", "crypto"
    },
    # Adding new broker: just add its entry here
    # "bybit": {"crypto", "future"},
    # "deribit": {"crypto", "option"},
}

# Fee tiers per broker (taker fee, used for routing preference)
BROKER_FEE_MAP = {
    "ibkr":    Decimal("0.0018"),   # ~0.18% crypto, ~$0.005/share equities
    "kraken":  Decimal("0.0040"),   # 0.40% taker base
    "binance": Decimal("0.0010"),   # 0.10% base
    "bybit":   Decimal("0.00055"),  # typical taker ~0.055% linear (tiered)
    "alpaca":  Decimal("0.0000"),   # zero commission equities
}

# Pseudo-observation count for fee→routing prior when blending with online quality (Wave 9).
ROUTING_PRIOR_PSEUDO_N = 8.0
_SLIP_WINDOW = 32


def _fee_prior_scores() -> dict[str, float]:
    """Map broker → [-1, 1] prior from relative taker fee (lower fee → higher score)."""
    vals = {k: float(v) for k, v in BROKER_FEE_MAP.items()}
    if not vals:
        return {}
    fmin = min(vals.values())
    fmax = max(vals.values())
    span = fmax - fmin + 1e-15
    out: dict[str, float] = {}
    for k, v in vals.items():
        prior = 1.0 - 2.0 * (v - fmin) / span
        out[k] = max(-1.0, min(1.0, prior))
    return out


FEE_PRIOR_SCORE = _fee_prior_scores()


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _slippage_percentiles_bps(samples: list[float]) -> tuple[float, float]:
    if not samples:
        return 0.0, 0.0
    xs = sorted(float(x) for x in samples if isinstance(x, (int, float)) and math.isfinite(float(x)))
    if not xs:
        return 0.0, 0.0

    def _pct(p: float) -> float:
        if len(xs) == 1:
            return xs[0]
        idx = int(round((p / 100.0) * (len(xs) - 1)))
        return xs[max(0, min(len(xs) - 1, idx))]

    return _pct(50.0), _pct(90.0)


class SmartOrderRouter:

    # ── Equity routing tuning ──────────────────────────────────────────────
    # Below this demand_score we keep equity orders on IBKR even in hunter
    # mode. Originally 0.35, which proved too strict in practice — the
    # learned router never sees Alpaca fills, so it can't accumulate
    # comparative execution-quality stats. 0.15 lets cheap Alpaca pick up
    # mildly risk-on flow while still defaulting to IBKR in flat regimes.
    ALPACA_HUNTER_DEMAND_THRESHOLD: float = 0.15

    # Probabilistic A/B split: even outside the hunter+demand path, route a
    # small slice of equity orders to Alpaca so the online quality model
    # has real comparative fill / slippage data. Without this, ``Alpaca``
    # is permanently penalised by a zero-evidence prior. Override per call
    # via ``metadata['equity_ab_split']``.
    EQUITY_ALPACA_AB_PROBABILITY: float = 0.20

    def __init__(self, available_brokers: list[str]):
        self.available_brokers = available_brokers
        self.permissions = get_permissions()
        # Learned execution quality map keyed by (broker, symbol_upper).
        # Positive score improves routing preference; negative penalizes.
        self._learned_quality: dict[tuple[str, str], float] = {}
        self._quality_history: dict[str, list[dict[str, float | str]]] = {}
        self._obs_stats: dict[tuple[str, str], dict[str, float | str]] = {}
        # Per (broker, symbol): rolling slippage |bps| samples + fill counts for telemetry / persistence.
        self._exec_metrics: dict[tuple[str, str], dict[str, object]] = {}
        # Deterministic RNG for A/B routing; seeded per-symbol so the same
        # symbol routes consistently within a session unless the seed key
        # changes (e.g. the operator flips ``equity_ab_split=0`` off).
        import random as _random
        self._ab_rng = _random.Random()

    def route(self, asset_class: str, symbol: str, metadata: dict | None = None) -> Optional[str]:
        """
        Return the best broker name for this asset class and symbol.
        Priority: availability → lowest fee → IBKR as tiebreaker.
        """

        # Filter to brokers that support this asset class
        eligible = [
            b for b in self.available_brokers
            if asset_class in BROKER_ASSET_MAP.get(b, set())
        ]

        if not eligible:
            logger.warning(
                "No broker available by asset map | asset_class=%s symbol=%s",
                asset_class,
                symbol,
            )
            return None

        permitted = [
            b for b in eligible if self.permissions.check_permission(b, asset_class)
        ]
        if not permitted:
            logger.warning(
                "No broker permitted for asset_class=%s symbol=%s | eligible=%s",
                asset_class,
                symbol,
                eligible,
            )
            return None

        sym_u = (symbol or "").strip().upper()
        md = metadata if isinstance(metadata, dict) else {}

        def _rank_key(b: str) -> tuple[Decimal, Decimal]:
            if hasattr(self.permissions, "get_taker_fee_bps"):
                yaml_fee = self.permissions.get_taker_fee_bps(b)
            else:
                yaml_fee = 0.0
            if yaml_fee > 0:
                taker_fee_bps = Decimal(str(yaml_fee))
            else:
                taker_fee_bps = BROKER_FEE_MAP.get(b, Decimal("0.0010")) * Decimal("10000")

            # slippage from rolling fills
            em = self._exec_metrics.get((b, sym_u), {})
            slips = [float(x) for x in (em.get("slips") or []) if isinstance(x, (int, float))]
            p50, _ = _slippage_percentiles_bps(slips)
            slippage_cost_bps = Decimal(str(p50)) if slips else Decimal("0.0")

            # borrow cost if short
            borrow_cost_bps = Decimal("0.0")
            side = str(md.get("side", "long")).strip().lower()
            if side in ("short", "sell"):
                if hasattr(self.permissions, "get_borrow_rate_annual_pct"):
                    yaml_borrow = self.permissions.get_borrow_rate_annual_pct(b)
                else:
                    yaml_borrow = 0.0
                if yaml_borrow > 0:
                    borrow_rate_annual = Decimal(str(yaml_borrow))
                else:
                    if hasattr(self.permissions, "get_default_annual_borrow_rate_pct"):
                        borrow_rate_annual = Decimal(str(self.permissions.get_default_annual_borrow_rate_pct()))
                    else:
                        borrow_rate_annual = Decimal("6.0")
                if hasattr(self.permissions, "get_default_hold_days"):
                    default_hold_days = float(self.permissions.get_default_hold_days())
                else:
                    default_hold_days = 5.0
                hold_days = float(md.get("hold_days", default_hold_days))
                borrow_cost_bps = (borrow_rate_annual * Decimal("100")) * (Decimal(str(hold_days)) / Decimal("365.0"))

            spread_bps = Decimal(str(md.get("spread_bps", 0.0)))
            total_cost = spread_bps + taker_fee_bps + slippage_cost_bps + borrow_cost_bps

            q = self.fused_routing_score(b, sym_u)
            return Decimal(str(-q)), total_cost

        permitted.sort(key=_rank_key)
        try:
            demand_score = float(md.get("demand_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            demand_score = 0.0
        profile_mode = str(md.get("profile_mode", "") or "").strip().lower()

        # IBKR is preferred for non-crypto (regulatory safety, multi-asset).
        # Two carve-outs for ``equity`` / ``etf`` keep Alpaca in the mix:
        #   (a) explicit "give cheap Alpaca the modestly-risk-on flow"
        #       path — threshold lowered from 0.35 to 0.15 because the old
        #       threshold was effectively never hit in normal markets.
        #   (b) a small probabilistic A/B slice so the online quality model
        #       actually has Alpaca evidence to compare against IBKR.
        if asset_class != "crypto" and "ibkr" in permitted:
            if (
                asset_class in {"equity", "etf"}
                and profile_mode == "hunter"
                and demand_score > self.ALPACA_HUNTER_DEMAND_THRESHOLD
                and "alpaca" in permitted
            ):
                return "alpaca"
            if (
                asset_class in {"equity", "etf"}
                and "alpaca" in permitted
                and _truthy(md.get("equity_ab_split", True))
            ):
                try:
                    ab_p = float(md.get("equity_ab_probability", self.EQUITY_ALPACA_AB_PROBABILITY))
                except (TypeError, ValueError):
                    ab_p = self.EQUITY_ALPACA_AB_PROBABILITY
                ab_p = max(0.0, min(1.0, ab_p))
                if ab_p > 0:
                    # Seed on symbol so same symbol routes consistently within a session.
                    self._ab_rng.seed(hash(sym_u) & 0xFFFFFFFF)
                    if self._ab_rng.random() < ab_p:
                        return "alpaca"
            return "ibkr"

        # Crypto perps / shorts: prefer Bybit when listed and permitted
        if asset_class == "future" and "bybit" in permitted:
            return "bybit"

        # Demand-aware spot crypto preference:
        # risk-on -> lower-fee Binance, risk-off -> Kraken resilience.
        if asset_class == "crypto":
            dedicated = [b for b in permitted if b in {"binance", "kraken", "bybit"}]
            if dedicated and not _truthy(md.get("allow_alpaca_crypto")):
                permitted = dedicated
                permitted.sort(key=_rank_key)
            fiat_usd_pair = sym_u.endswith("-USD") or sym_u.endswith("/USD")
            if fiat_usd_pair and "kraken" in permitted and not _truthy(md.get("allow_usd_stablecoin_conversion")):
                try:
                    from system.paper_wallet import venue_deploy_room

                    room = venue_deploy_room("kraken")
                except Exception:  # noqa: BLE001
                    room = None
                if room is None or room > 0:
                    return "kraken"
                # Canonical *-USD crypto can use USDT books when Kraken has no
                # deployable room. Prefer Binance spot before Bybit because
                # Bybit availability may represent derivatives/perp access.
                converted = [b for b in ("binance", "bybit") if b in permitted]
                for broker in converted:
                    try:
                        broker_room = venue_deploy_room(broker)
                    except Exception:  # noqa: BLE001
                        broker_room = None
                    if broker_room is None or broker_room > 0:
                        return broker
                return "kraken"
            if not _truthy(md.get("allow_bybit_spot_usd")):
                if fiat_usd_pair:
                    permitted = [b for b in permitted if b != "bybit"]
                elif not (sym_u.endswith("-USDT") or sym_u.endswith("/USDT") or sym_u.endswith("-USDC") or sym_u.endswith("/USDC")):
                    permitted = [b for b in permitted if b != "bybit"]
            if not permitted:
                logger.warning(
                    "No crypto venue compatible with symbol=%s after quote filtering",
                    symbol,
                )
                return None
            if demand_score >= 0.45 and "binance" in permitted:
                return "binance"
            if demand_score <= -0.45 and "kraken" in permitted:
                return "kraken"

        chosen = permitted[0]
        logger.debug("Routing %s (%s) -> %s", symbol, asset_class, chosen)
        return chosen

    def record_execution_feedback(
        self,
        *,
        broker: str,
        symbol: str,
        filled: bool,
        slippage_bps: float | None = None,
        turnover_hint: float | None = None,
        liquidity_hint: float | None = None,
    ) -> None:
        b = (broker or "").strip().lower()
        s = (symbol or "").strip().upper()
        if not b or not s:
            return
        prev = float(self._learned_quality.get((b, s), 0.0))
        # Fill quality core signal.
        fill_term = 0.08 if filled else -0.08
        # Slippage penalty/bonus (smaller is better). Clip to avoid spikes.
        slip_term = 0.0
        if slippage_bps is not None:
            try:
                sb = float(slippage_bps)
                slip_term = max(-0.12, min(0.08, -sb / 200.0))
            except (TypeError, ValueError):
                slip_term = 0.0
        updated = prev * 0.92 + fill_term + slip_term
        self._learned_quality[(b, s)] = max(-1.0, min(1.0, updated))
        key = (b, s)
        st = dict(self._obs_stats.get(key, {}))
        n_prev = int(float(st.get("n", 0.0) or 0.0))
        mean_prev = float(st.get("mean", 0.0) or 0.0)
        m2_prev = float(st.get("m2", 0.0) or 0.0)
        x = float(self._learned_quality[key])
        n_new = n_prev + 1
        delta = x - mean_prev
        mean_new = mean_prev + delta / n_new
        delta2 = x - mean_new
        m2_new = m2_prev + delta * delta2
        # Execution activity proxies for adaptive decay.
        try:
            t_hint = float(turnover_hint) if turnover_hint is not None else 0.0
        except (TypeError, ValueError):
            t_hint = 0.0
        try:
            l_hint = float(liquidity_hint) if liquidity_hint is not None else 0.0
        except (TypeError, ValueError):
            l_hint = 0.0
        prev_t = float(st.get("turnover_ema", 0.0) or 0.0)
        prev_l = float(st.get("liquidity_ema", 0.0) or 0.0)
        st["n"] = float(n_new)
        st["mean"] = mean_new
        st["m2"] = max(0.0, m2_new)
        st["turnover_ema"] = prev_t * 0.9 + max(0.0, t_hint) * 0.1
        st["liquidity_ema"] = prev_l * 0.9 + max(0.0, l_hint) * 0.1
        st["last_ts"] = datetime.now(timezone.utc).isoformat()
        self._obs_stats[key] = st
        em = dict(self._exec_metrics.get(key, {}))
        slips: list[float] = [float(x) for x in (em.get("slips") or []) if isinstance(x, (int, float))]
        attempts = int(em.get("attempts", 0) or 0) + 1
        fills_ct = int(em.get("fills", 0) or 0)
        if filled:
            fills_ct += 1
        em["attempts"] = attempts
        em["fills"] = fills_ct
        if slippage_bps is not None:
            try:
                slips.append(abs(float(slippage_bps)))
            except (TypeError, ValueError):
                pass
        em["slips"] = slips[-_SLIP_WINDOW:]
        self._exec_metrics[key] = em
        hist = self._quality_history.setdefault(s, [])
        hist.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "broker": b,
                "score": round(float(self._learned_quality[(b, s)]), 6),
            }
        )
        self._quality_history[s] = hist[-60:]

    def apply_decay(self, rate: float = 0.02, *, adaptive: bool = True) -> None:
        """Exponential decay toward neutral for learned routing quality.

        Adaptive mode decays stale/low-activity symbols faster and liquid/high-turnover
        symbols slower.
        """
        r = max(0.0, min(0.5, float(rate)))
        if r <= 0:
            return
        now = datetime.now(timezone.utc)
        for k, v in list(self._learned_quality.items()):
            eff = r
            if adaptive:
                st = self._obs_stats.get(k, {})
                n = float(st.get("n", 0.0) or 0.0)
                t_ema = float(st.get("turnover_ema", 0.0) or 0.0)
                l_ema = float(st.get("liquidity_ema", 0.0) or 0.0)
                ts_raw = str(st.get("last_ts", "") or "")
                age_h = 0.0
                try:
                    if ts_raw:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                        age_h = max(0.0, (now - ts).total_seconds() / 3600.0)
                except Exception:  # noqa: BLE001
                    age_h = 0.0
                sample_factor = 1.0 / (1.0 + min(100.0, n) / 15.0)
                turnover_factor = 1.0 / (1.0 + max(0.0, t_ema) / 50000.0)
                liq_factor = 1.0 / (1.0 + max(0.0, l_ema))
                staleness_boost = min(2.0, 1.0 + age_h / 24.0)
                eff = r * (0.7 + 0.6 * sample_factor + 0.4 * turnover_factor + 0.3 * liq_factor) * staleness_boost
                eff = max(r * 0.35, min(r * 3.0, eff))
            self._learned_quality[k] = float(v) * (1.0 - eff)

    def fused_routing_score(self, broker: str, symbol: str) -> float:
        """Bayesian-style blend: fee prior (pseudo-obs) + mean learned score weighted by n."""
        b = (broker or "").strip().lower()
        s = (symbol or "").strip().upper()
        prior = float(FEE_PRIOR_SCORE.get(b, 0.0))
        learned = float(self._learned_quality.get((b, s), 0.0))
        st = self._obs_stats.get((b, s), {})
        n = max(0.0, min(100.0, float(st.get("n", 0.0) or 0.0)))
        w0 = ROUTING_PRIOR_PSEUDO_N
        fused = (w0 * prior + n * learned) / (w0 + n) if (w0 + n) > 0 else prior
        return max(-1.0, min(1.0, float(fused)))

    def export_quality_state(self) -> dict[str, object]:
        map_out: dict[str, dict[str, float]] = {}
        stats_out: dict[str, dict[str, dict[str, float]]] = {}
        all_keys = set(self._learned_quality.keys()) | set(self._exec_metrics.keys())
        for (broker, symbol) in sorted(all_keys, key=lambda k: (k[1], k[0])):
            score = float(self._learned_quality.get((broker, symbol), 0.0))
            by_sym = map_out.setdefault(symbol, {})
            by_sym[broker] = round(score, 6)
            st = self._obs_stats.get((broker, symbol), {})
            n = max(0.0, float(st.get("n", 0.0) or 0.0))
            m2 = max(0.0, float(st.get("m2", 0.0) or 0.0))
            var = (m2 / (n - 1.0)) if n > 1.0 else 0.0
            std = math.sqrt(max(0.0, var))
            se = std / math.sqrt(n) if n > 0 else 0.0
            ci95_half = 1.96 * se
            em = self._exec_metrics.get((broker, symbol), {})
            slips = [float(x) for x in (em.get("slips") or []) if isinstance(x, (int, float))]
            p50, p90 = _slippage_percentiles_bps(slips)
            attempts = max(0, int(em.get("attempts", 0) or 0))
            fills = max(0, int(em.get("fills", 0) or 0))
            fill_rate = (fills / attempts) if attempts > 0 else 0.0
            fee_prior = float(FEE_PRIOR_SCORE.get(broker, 0.0))
            fused = self.fused_routing_score(broker, symbol)
            srow = stats_out.setdefault(symbol, {})
            srow[broker] = {
                "n": round(n, 3),
                "std": round(std, 6),
                "ci95_half": round(ci95_half, 6),
                "turnover_ema": round(float(st.get("turnover_ema", 0.0) or 0.0), 6),
                "liquidity_ema": round(float(st.get("liquidity_ema", 0.0) or 0.0), 6),
                "fee_prior": round(fee_prior, 6),
                "fused_score": round(fused, 6),
                "p50_slippage_bps": round(p50, 4),
                "p90_slippage_bps": round(p90, 4),
                "fill_rate": round(fill_rate, 4),
                "exec_attempts": float(attempts),
                "exec_fills": float(fills),
            }
        broker_rows: list[dict[str, float | str]] = []
        for (broker, symbol) in sorted(all_keys, key=lambda k: (k[1], k[0])):
            st = stats_out.get(symbol, {}).get(broker, {})
            broker_rows.append(
                {
                    "symbol": symbol,
                    "broker": broker,
                    "learned_score": float(map_out.get(symbol, {}).get(broker, 0.0)),
                    "fused_score": float(st.get("fused_score", 0.0) or 0.0),
                    "fee_prior": float(st.get("fee_prior", 0.0) or 0.0),
                    "ci95_half": float(st.get("ci95_half", 0.0) or 0.0),
                    "n": float(st.get("n", 0.0) or 0.0),
                    "p50_slippage_bps": float(st.get("p50_slippage_bps", 0.0) or 0.0),
                    "p90_slippage_bps": float(st.get("p90_slippage_bps", 0.0) or 0.0),
                    "fill_rate": float(st.get("fill_rate", 0.0) or 0.0),
                    "exec_attempts": float(st.get("exec_attempts", 0.0) or 0.0),
                    "exec_fills": float(st.get("exec_fills", 0.0) or 0.0),
                }
            )
        broker_rows.sort(key=lambda r: (-float(r["fused_score"]), str(r["symbol"]), str(r["broker"])))
        exec_out: dict[str, dict[str, dict[str, object]]] = {}
        for (broker, symbol), em in self._exec_metrics.items():
            slips = [float(x) for x in (em.get("slips") or []) if isinstance(x, (int, float))]
            exec_out.setdefault(symbol, {})[broker] = {
                "slips": slips[-_SLIP_WINDOW:],
                "attempts": int(em.get("attempts", 0) or 0),
                "fills": int(em.get("fills", 0) or 0),
            }
        return {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "quality_map": map_out,
            "quality_stats": stats_out,
            "history": dict(self._quality_history),
            "broker_comparison": broker_rows,
            "exec_metrics": exec_out,
        }

    def import_quality_state(self, data: dict | None) -> None:
        if not isinstance(data, dict):
            return
        qmap = data.get("quality_map")
        if isinstance(qmap, dict):
            loaded: dict[tuple[str, str], float] = {}
            for sym, by_broker in qmap.items():
                if not isinstance(by_broker, dict):
                    continue
                s = str(sym).strip().upper()
                if not s:
                    continue
                for b, v in by_broker.items():
                    try:
                        vv = float(v)
                    except (TypeError, ValueError):
                        continue
                    loaded[(str(b).strip().lower(), s)] = max(-1.0, min(1.0, vv))
            self._learned_quality = loaded
        qstats = data.get("quality_stats")
        if isinstance(qstats, dict):
            loaded_stats: dict[tuple[str, str], dict[str, float | str]] = {}
            for sym, by_broker in qstats.items():
                if not isinstance(by_broker, dict):
                    continue
                s = str(sym).strip().upper()
                if not s:
                    continue
                for b, row in by_broker.items():
                    if not isinstance(row, dict):
                        continue
                    key = (str(b).strip().lower(), s)
                    loaded_stats[key] = {
                        "n": float(row.get("n", 0.0) or 0.0),
                        "m2": 0.0,  # not recoverable from exported std exactly; decay-safe default
                        "mean": float(self._learned_quality.get(key, 0.0)),
                        "turnover_ema": float(row.get("turnover_ema", 0.0) or 0.0),
                        "liquidity_ema": float(row.get("liquidity_ema", 0.0) or 0.0),
                        "last_ts": datetime.now(timezone.utc).isoformat(),
                    }
            self._obs_stats = loaded_stats
        ex = data.get("exec_metrics")
        if isinstance(ex, dict):
            loaded_ex: dict[tuple[str, str], dict[str, object]] = {}
            for sym, by_broker in ex.items():
                if not isinstance(by_broker, dict):
                    continue
                s = str(sym).strip().upper()
                if not s:
                    continue
                for b, row in by_broker.items():
                    if not isinstance(row, dict):
                        continue
                    key = (str(b).strip().lower(), s)
                    slips_raw = row.get("slips")
                    slips: list[float] = []
                    if isinstance(slips_raw, list):
                        for x in slips_raw[-_SLIP_WINDOW:]:
                            try:
                                slips.append(float(x))
                            except (TypeError, ValueError):
                                continue
                    loaded_ex[key] = {
                        "slips": slips,
                        "attempts": int(row.get("attempts", 0) or 0),
                        "fills": int(row.get("fills", 0) or 0),
                    }
            self._exec_metrics = loaded_ex
        hist = data.get("history")
        if isinstance(hist, dict):
            cleaned: dict[str, list[dict[str, float | str]]] = {}
            for sym, rows in hist.items():
                if not isinstance(rows, list):
                    continue
                s = str(sym).strip().upper()
                if not s:
                    continue
                out_rows: list[dict[str, float | str]] = []
                for r in rows[-60:]:
                    if not isinstance(r, dict):
                        continue
                    broker = str(r.get("broker", "")).strip().lower()
                    ts = str(r.get("ts", "")).strip()
                    try:
                        score = float(r.get("score", 0.0))
                    except (TypeError, ValueError):
                        score = 0.0
                    if not broker:
                        continue
                    out_rows.append({"ts": ts, "broker": broker, "score": round(max(-1.0, min(1.0, score)), 6)})
                if out_rows:
                    cleaned[s] = out_rows
            self._quality_history = cleaned

    def check_permission(self, broker: str, asset_class: str) -> bool:
        return self.permissions.check_permission(broker, asset_class)

    def get_fallback_broker(self, asset_class: str, exclude: list[str] | None = None) -> Optional[str]:
        candidates = [
            b
            for b in self.available_brokers
            if asset_class in BROKER_ASSET_MAP.get(b, set())
        ]
        return self.permissions.get_fallback_broker(
            asset_class,
            candidates=candidates,
            exclude=exclude or [],
        )

    def reload_permissions(self) -> None:
        self.permissions.reload(force=True)

    def add_broker(self, name: str) -> None:
        """Register a newly connected broker as available for routing."""
        if name not in self.available_brokers:
            self.available_brokers.append(name)
            logger.info(f"Router: added broker {name}")
