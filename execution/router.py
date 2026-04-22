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


class SmartOrderRouter:

    def __init__(self, available_brokers: list[str]):
        self.available_brokers = available_brokers
        self.permissions = get_permissions()
        # Learned execution quality map keyed by (broker, symbol_upper).
        # Positive score improves routing preference; negative penalizes.
        self._learned_quality: dict[tuple[str, str], float] = {}
        self._quality_history: dict[str, list[dict[str, float | str]]] = {}
        self._obs_stats: dict[tuple[str, str], dict[str, float | str]] = {}

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
        # Sort by fee and learned execution quality.
        # Better learned quality reduces effective rank value.
        def _rank_key(b: str) -> tuple[Decimal, float]:
            fee = BROKER_FEE_MAP.get(b, Decimal("0.01"))
            q = float(self._learned_quality.get((b, sym_u), 0.0))
            return fee, -q

        permitted.sort(key=_rank_key)

        md = metadata if isinstance(metadata, dict) else {}
        try:
            demand_score = float(md.get("demand_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            demand_score = 0.0
        profile_mode = str(md.get("profile_mode", "") or "").strip().lower()

        # IBKR is preferred for non-crypto (regulatory safety, multi-asset)
        if asset_class != "crypto" and "ibkr" in permitted:
            # In strong risk-on hunter mode for equities, allow cheaper Alpaca.
            if asset_class in {"equity", "etf"} and profile_mode == "hunter" and demand_score > 0.35 and "alpaca" in permitted:
                return "alpaca"
            return "ibkr"

        # Crypto perps / shorts: prefer Bybit when listed and permitted
        if asset_class == "future" and "bybit" in permitted:
            return "bybit"

        # Demand-aware spot crypto preference:
        # risk-on -> lower-fee Binance, risk-off -> Kraken resilience.
        if asset_class == "crypto":
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

    def export_quality_state(self) -> dict[str, object]:
        map_out: dict[str, dict[str, float]] = {}
        stats_out: dict[str, dict[str, dict[str, float]]] = {}
        for (broker, symbol), score in self._learned_quality.items():
            by_sym = map_out.setdefault(symbol, {})
            by_sym[broker] = round(float(score), 6)
            st = self._obs_stats.get((broker, symbol), {})
            n = max(0.0, float(st.get("n", 0.0) or 0.0))
            m2 = max(0.0, float(st.get("m2", 0.0) or 0.0))
            var = (m2 / (n - 1.0)) if n > 1.0 else 0.0
            std = math.sqrt(max(0.0, var))
            se = std / math.sqrt(n) if n > 0 else 0.0
            ci95_half = 1.96 * se
            srow = stats_out.setdefault(symbol, {})
            srow[broker] = {
                "n": round(n, 3),
                "std": round(std, 6),
                "ci95_half": round(ci95_half, 6),
                "turnover_ema": round(float(st.get("turnover_ema", 0.0) or 0.0), 6),
                "liquidity_ema": round(float(st.get("liquidity_ema", 0.0) or 0.0), 6),
            }
        return {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "quality_map": map_out,
            "quality_stats": stats_out,
            "history": dict(self._quality_history),
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
