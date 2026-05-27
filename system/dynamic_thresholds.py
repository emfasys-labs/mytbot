"""
system/dynamic_thresholds.py
=============================
**Live formulas for every Category-C tuning constant.**

The system used to store strategy thresholds as fixed YAML values:

    rsi_buy_threshold: 47
    rsi_sell_threshold: 53
    band_epsilon: 0.006
    volume_multiplier: 1.2
    momentum_threshold: 0.001

Each of those is the same number whether the market is gentle or
violent, whether the strategy is in its native regime or fighting it.
That's not adaptive — that's a hard-coded preference.

This module replaces every such constant with a **function of live
market features and strategy performance**. Strategies pull thresholds
through here per tick; thresholds change continuously with conditions.

What stays in YAML are the **safety coefficients** — the operator's
upper / lower bounds on how far a formula can move a threshold. That
limits the system's room to surprise you, without freezing it.

What stays in code are pure mathematical constants:
  * the RSI midpoint (50) — fixed by the RSI definition itself
  * the multiplicative identity (1.0)
  * the [0,1] midpoint (0.5) — the natural feature-centering point

Every other tuning value is derived. The functions here form the
building blocks; ``apply_*`` callers wire them into the strategy code.

Dependency direction:
  * primitives  ──  live market features (RegimeState.components,
                    per-symbol df features, recent fills ledger)
  * primitives  ──  YAML safety coefficients
  * formulas    ──  combine primitives → emit threshold
  * strategies  ──  consume the formula output, not raw YAML
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from loguru import logger


# ── Required-key contract per formula group ────────────────────────────────
# When the ``dynamic_thresholds`` block is ``enabled: true`` but a group is
# missing any of its required keys, that group is dropped from the live
# config with a CRITICAL log. The strategy then falls back to the legacy
# static value (passthrough), so a misconfig can never silently activate
# a half-formula with code-baked coefficients.
_REQUIRED_KEYS: dict[str, set[str]] = {
    "rsi": {
        "base_distance", "vol_weight", "trend_weight",
        "min_distance", "max_distance",
    },
    "bollinger_epsilon": {
        "atr_multiple", "min_epsilon", "max_epsilon",
    },
    "momentum_breakout": {
        "atr_multiple", "min_threshold", "max_threshold",
    },
    "volume_confirmation": {
        "base_multiplier", "z_weight", "min_multiplier", "max_multiplier",
    },
    "sizing": {
        "base_nav_pct", "pnl_health_weight", "min_nav_pct", "max_nav_pct",
    },
    "volume_zscore": {
        "open_base", "open_atr_weight", "open_min", "open_max",
        "exhaust_base", "exhaust_atr_weight", "exhaust_min", "exhaust_max",
    },
    "min_bar_return": {
        "atr_multiple", "min_threshold", "max_threshold",
    },
    "atr_band": {
        "lower_multiple", "upper_multiple", "abs_min", "abs_max",
    },
    "event_shock": {
        "base", "dispersion_weight", "min_threshold", "max_threshold",
    },
    "pairs_zscore": {
        "base", "health_weight", "min_threshold", "max_threshold",
    },
    "regime_rotation": {
        "base", "clarity_weight", "min_threshold", "max_threshold",
    },
    "anti_churn_cooldown": {
        "base_by_mode", "base_default", "clarity_weight",
        "fill_rate_weight", "min_sec", "max_sec",
    },
}

# Track which (block_mtime, group) failures we've already logged so we
# log once per misconfig instead of every tick.
_LOGGED_INVALID: set[tuple[float, str]] = set()


def _stamp_hash(payload: Any) -> str:
    """Stable 12-char SHA-256 prefix for any JSON-serialisable payload."""
    try:
        blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    except Exception:  # noqa: BLE001
        return ""
    return hashlib.sha256(blob).hexdigest()[:12]


def build_thresholds_snapshot(
    *,
    market_features: dict[str, Any] | None,
    market_state_score: Any,
    representative_atr_pct: Any = "0.01",
    median_atr_pct: Any = "0.01",
    nav: Any = 0,
    strategy_pnl_recent: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a JSON-serialisable summary of every live dynamic threshold.

    Used by the dashboard publisher to show operators what the formulas
    resolved to in the current tick, and (when paired with the
    ``config_hash`` stamp on signals) why each signal carried the
    threshold it did. All numbers come from the same formulas the live
    strategies consume.

    Inputs default to neutral so the function is safe to call even when
    the live trading loop hasn't populated everything yet.
    """
    from system.adaptive_regime_weights import compute_multiplier

    feats = market_features or {}
    pnl_map = strategy_pnl_recent or {}

    rsi_buy, rsi_sell = rsi_thresholds(
        atr_pct=representative_atr_pct,
        market_state_score=market_state_score,
    )

    out: dict[str, Any] = {
        "config_hash": config_version(),
        "rsi": {"buy": float(rsi_buy), "sell": float(rsi_sell)},
        "bollinger_epsilon": float(bollinger_band_epsilon(atr_pct=representative_atr_pct)),
        "momentum_breakout_threshold": float(momentum_breakout_threshold(atr_pct=representative_atr_pct)),
        "volume_confirmation_multiplier": float(
            volume_confirmation_multiplier(volume_z_recent=0)
        ),
        "volume_zscore_open": float(volume_zscore_open_threshold(atr_pct=representative_atr_pct)),
        "volume_zscore_exhaust": float(volume_zscore_exhaust_threshold(atr_pct=representative_atr_pct)),
        "min_bar_return": float(min_bar_return_threshold(atr_pct=representative_atr_pct)),
        "atr_band": [
            float(acceptable_atr_band(median_atr_pct=median_atr_pct)[0]),
            float(acceptable_atr_band(median_atr_pct=median_atr_pct)[1]),
        ],
        "event_shock_threshold": float(event_shock_threshold(news_score_dispersion=feats.get("news_conflict_score", 0))),
        "pairs_zscore_open": float(pairs_zscore_open_threshold(cointegration_health=feats.get("correlation_crowding", 0.5))),
        "regime_rotation_trigger": float(regime_rotation_score_trigger(market_state_score=market_state_score)),
        "anti_churn_cooldown_sec_by_mode": {
            mode: float(anti_churn_cooldown_sec(
                mode=mode,
                market_state_score=market_state_score,
                recent_fill_rate_per_min=0,
            ))
            for mode in ("hunter", "trader", "defender")
        },
    }

    # Per-strategy resolved (regime mult, sample sizing).
    per_strategy: dict[str, Any] = {}
    for strat in (
        "mean_reversion", "momentum_breakout", "volume_flow",
        "volatility_regime", "event_driven_news", "pairs_trading",
        "regime_rotation",
    ):
        regime_mult = compute_multiplier(strat, feats)
        stats = pnl_map.get(strat, {})
        sample_size = base_target_notional(
            nav=nav,
            strategy_net_pnl_recent=stats.get("net_pnl", 0),
            strategy_total_fills_recent=stats.get("fills", 0),
            regime_multiplier=regime_mult,
            static_notional=0,
        )
        per_strategy[strat] = {
            "regime_multiplier": float(regime_mult),
            "recent_net_pnl": float(stats.get("net_pnl", 0) or 0),
            "recent_fills": int(stats.get("fills", 0) or 0),
            "recent_win_rate": float(stats.get("win_rate", 0) or 0),
            "sample_target_notional": float(sample_size),
        }
    out["per_strategy"] = per_strategy
    return out

# ── Mathematical identities (NOT tuning) ────────────────────────────────────
_RSI_MIDPOINT = Decimal("50")              # RSI's natural neutral point
_FEATURE_MIDPOINT = Decimal("0.5")         # midpoint of [0,1] features
_NEUTRAL = Decimal("1.0")                  # multiplicative identity

_CONFIG_PATH = Path("config/strategies.yaml")
_YAML_CACHE: tuple[float, dict[str, Any]] | None = None


def config_version() -> str:
    """Stable 12-char hash of the currently-active ``dynamic_thresholds``
    + ``regime_weights`` YAML blocks.

    Every signal produced under the same config has the same hash; any
    operator edit changes the hash within one mtime cycle. Used for
    P&L attribution — a fill row carrying ``config_hash = "ab12cd..."``
    can be tied back to the exact threshold regime in effect when it
    was generated.

    Returns an empty string if YAML cannot be read (degraded behaviour;
    callers should treat empty hash as "config unknown").
    """
    try:
        with _CONFIG_PATH.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001
        return ""
    relevant = {
        "dynamic_thresholds": data.get("dynamic_thresholds"),
        "regime_weights": data.get("regime_weights"),
    }
    return _stamp_hash(relevant)


def _validate_group(mtime: float, name: str, group: dict[str, Any]) -> bool:
    """Return True if ``group`` has every required key (per ``_REQUIRED_KEYS``).

    On failure, log CRITICAL ONCE per (mtime, name) so a stuck misconfig
    doesn't spam the log every tick.
    """
    required = _REQUIRED_KEYS.get(name)
    if required is None:
        # Unknown group — leave it alone (operator may be staging a new formula).
        return True
    missing = [k for k in required if k not in group]
    if not missing:
        return True
    key = (mtime, name)
    if key not in _LOGGED_INVALID:
        _LOGGED_INVALID.add(key)
        logger.critical(
            "dynamic_thresholds | group {!r} missing required keys {} — "
            "this formula DISABLED; strategies fall back to legacy static values. "
            "Either add the keys or remove the group entirely.",
            name,
            sorted(missing),
        )
    return False


def _load_dynamic_block() -> dict[str, Any]:
    """Return the validated ``dynamic_thresholds`` block from strategies.yaml.

    Resolution policy (fail-closed, no silent invention):
      * Block absent / disabled / unreadable → {} (every formula passthrough).
      * Block present, group with all required keys → group retained.
      * Block present, group with MISSING required keys → group **stripped**
        from the returned config and a CRITICAL log emitted once. The
        affected formulas then return their static-fallback path, never
        a partially-computed value off code-baked defaults.

    This makes "enabled but incomplete" fail loudly without crashing the
    trading loop. A typo in YAML cannot silently activate a half-formula.
    """
    global _YAML_CACHE
    try:
        mtime = _CONFIG_PATH.stat().st_mtime
    except OSError:
        return {}
    if _YAML_CACHE is not None and _YAML_CACHE[0] == mtime:
        return _YAML_CACHE[1]
    try:
        with _CONFIG_PATH.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001 — never break the trading loop on YAML
        _YAML_CACHE = (mtime, {})
        return {}
    block = data.get("dynamic_thresholds") or {}
    if not isinstance(block, dict) or not block.get("enabled", False):
        _YAML_CACHE = (mtime, {})
        return {}

    # Strict validation: keep only groups whose required keys are all
    # present. Everything else is dropped and CRITICAL-logged once.
    validated: dict[str, Any] = {"enabled": True}
    for name, value in block.items():
        if name == "enabled":
            continue
        if not isinstance(value, dict):
            validated[name] = value
            continue
        if _validate_group(mtime, name, value):
            validated[name] = value
        # else: silently strip — the CRITICAL log already flagged it.
    _YAML_CACHE = (mtime, validated)
    return validated


def _coef(group: str, key: str) -> Decimal | None:
    """Pull a single safety coefficient from YAML. Returns None when missing."""
    block = _load_dynamic_block()
    grp = block.get(group)
    if not isinstance(grp, dict):
        return None
    raw = grp.get(key)
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _decimal_or(v: Any, fallback: Decimal) -> Decimal:
    """Best-effort Decimal cast with a structural fallback (not a tuning default)."""
    if v is None:
        return fallback
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return fallback


def _strict_decimal(v: Any) -> Decimal | None:
    """Strict Decimal cast — returns ``None`` on any parse failure so the
    caller can fail closed instead of substituting a code-baked default."""
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _strict_pull(cfg: dict, *keys: str) -> tuple[Decimal, ...] | None:
    """Pull every requested key as Decimal. Returns ``None`` if any key
    is absent or unparseable so the calling formula can fall back to
    its static-passthrough path. No tuning values baked in code."""
    out: list[Decimal] = []
    for k in keys:
        d = _strict_decimal(cfg.get(k))
        if d is None:
            return None
        out.append(d)
    return tuple(out)


def _clamp(value: Decimal, lo: Decimal | None, hi: Decimal | None) -> Decimal:
    if lo is not None and value < lo:
        return lo
    if hi is not None and value > hi:
        return hi
    return value


# ─────────────────────────────────────────────────────────────────────────────
# Mean-reversion: RSI thresholds + Bollinger-band proximity
# ─────────────────────────────────────────────────────────────────────────────


def rsi_thresholds(
    *,
    atr_pct: Any,
    market_state_score: Any = 0,
    static_buy_threshold: Any | None = None,
    static_sell_threshold: Any | None = None,
) -> tuple[Decimal, Decimal]:
    """Compute the (buy, sell) RSI thresholds for mean-reversion **live**.

    Static config used 47/53 — fires on any tiny oscillation regardless of
    volatility or regime. The formula expands the entry band when:
      * ``atr_pct`` is high  (volatile market: need a deeper extreme)
      * ``|market_state_score|`` is high  (strong directional regime: don't
        catch falling knives or fade roaring trends)

    Both effects pull the buy threshold down (deeper oversold required) and
    push the sell threshold up (deeper overbought required), preserving
    symmetry around the RSI midpoint of 50.

    ``static_buy_threshold`` / ``static_sell_threshold`` are the legacy YAML
    values; they are used ONLY when the ``dynamic_thresholds.rsi`` YAML
    block is absent or disabled, so the formula activates by opting in,
    not by surprise.
    """
    block = _load_dynamic_block()
    cfg = block.get("rsi") if isinstance(block.get("rsi"), dict) else None
    static_pair = (
        _decimal_or(static_buy_threshold, Decimal("47")),
        _decimal_or(static_sell_threshold, Decimal("53")),
    )
    if not cfg:
        return static_pair
    coefs = _strict_pull(
        cfg, "base_distance", "vol_weight", "trend_weight",
        "min_distance", "max_distance",
    )
    if coefs is None:
        return static_pair
    base_distance, vol_weight, trend_weight, min_distance, max_distance = coefs

    atr = _decimal_or(atr_pct, Decimal("0"))
    if atr < 0:
        atr = Decimal("0")
    score_abs = abs(_decimal_or(market_state_score, Decimal("0")))

    distance = base_distance + vol_weight * atr + trend_weight * score_abs
    distance = _clamp(distance, min_distance, max_distance)

    buy = _RSI_MIDPOINT - distance
    sell = _RSI_MIDPOINT + distance
    return (buy, sell)


def bollinger_band_epsilon(
    *,
    atr_pct: Any,
    static_epsilon: Any | None = None,
) -> Decimal:
    """Bollinger-band proximity threshold, expressed as a fraction of price.

    Static config used 0.006 (0.6% of price). At high volatility that's
    inside one bar's range — the mean-reversion gate fires on noise. The
    dynamic formula expresses the threshold as a multiple of the symbol's
    realised ATR%, so the gate tightens / loosens with the symbol's own
    volatility regime.

    No legacy value is invented if the dynamic block is missing — the
    caller's existing YAML value (``static_epsilon``) is returned unchanged.
    """
    cfg = _load_dynamic_block().get("bollinger_epsilon")
    static_val = _decimal_or(static_epsilon, Decimal("0.006"))
    if not isinstance(cfg, dict):
        return static_val
    coefs = _strict_pull(cfg, "atr_multiple", "min_epsilon", "max_epsilon")
    if coefs is None:
        return static_val
    atr_multiple, floor, ceil = coefs
    atr = _decimal_or(atr_pct, Decimal("0"))
    if atr < 0:
        atr = Decimal("0")
    return _clamp(atr_multiple * atr, floor, ceil)


# ─────────────────────────────────────────────────────────────────────────────
# Momentum / volume-flow: breakout thresholds + volume confirmation
# ─────────────────────────────────────────────────────────────────────────────


def momentum_breakout_threshold(
    *,
    atr_pct: Any,
    static_threshold: Any | None = None,
) -> Decimal:
    """Minimum relative break above rolling high to qualify as momentum.

    Static config used 0.001 (10 bps). For a high-vol name (ATR 5%) that's
    noise; for a low-vol name (ATR 0.3%) it's a real breakout. The dynamic
    formula scales the threshold with the symbol's ATR — same number of
    "vol units" required regardless of name.
    """
    cfg = _load_dynamic_block().get("momentum_breakout")
    static_val = _decimal_or(static_threshold, Decimal("0.001"))
    if not isinstance(cfg, dict):
        return static_val
    coefs = _strict_pull(cfg, "atr_multiple", "min_threshold", "max_threshold")
    if coefs is None:
        return static_val
    atr_multiple, floor, ceil = coefs
    atr = _decimal_or(atr_pct, Decimal("0"))
    if atr < 0:
        atr = Decimal("0")
    return _clamp(atr_multiple * atr, floor, ceil)


def volume_confirmation_multiplier(
    *,
    volume_z_recent: Any = 0,
    static_multiplier: Any | None = None,
) -> Decimal:
    """How many times the rolling-mean volume is required to confirm a setup.

    Static config used 1.2 (very loose — passes on a 20% volume spike). The
    dynamic formula widens when the volume distribution is itself volatile
    (high recent z-score variance), tightens in steady markets. Same
    "rarity" of volume spike required regardless of the symbol's flow regime.
    """
    cfg = _load_dynamic_block().get("volume_confirmation")
    static_val = _decimal_or(static_multiplier, Decimal("1.2"))
    if not isinstance(cfg, dict):
        return static_val
    coefs = _strict_pull(
        cfg, "base_multiplier", "z_weight", "min_multiplier", "max_multiplier",
    )
    if coefs is None:
        return static_val
    base, z_weight, floor, ceil = coefs
    z = abs(_decimal_or(volume_z_recent, Decimal("0")))
    return _clamp(base + z_weight * z, floor, ceil)


# ─────────────────────────────────────────────────────────────────────────────
# Sizing: base notional that adapts to NAV + strategy P&L health + regime
# ─────────────────────────────────────────────────────────────────────────────


def base_target_notional(
    *,
    nav: Any,
    strategy_net_pnl_recent: Any = 0,
    strategy_total_fills_recent: Any = 0,
    regime_multiplier: Any = 1,
    quarantine_multiplier: Any = 1,
    static_notional: Any | None = None,
) -> Decimal:
    """The per-trade target notional, **derived from live state**:

      * scales with NAV (so $1.2M and $12M accounts trade proportionally)
      * shrinks when the strategy's recent net P&L is negative (a "P&L
        health" multiplier — bleeding strategies size down automatically)
      * scales with the regime multiplier already computed by
        :mod:`system.adaptive_regime_weights`
      * composes with the rolling strategy-quarantine multiplier

    Static config used 20_000 / 25_000 — the same dollar size on every
    trade regardless of account size or strategy performance. That's the
    "broken account compounding loser" pattern the audit traced.
    """
    cfg = _load_dynamic_block().get("sizing")
    static_val = _decimal_or(static_notional, Decimal("0"))
    if not isinstance(cfg, dict):
        return static_val
    coefs = _strict_pull(
        cfg, "base_nav_pct", "pnl_health_weight", "min_nav_pct", "max_nav_pct",
    )
    if coefs is None:
        return static_val
    base_pct, pnl_weight, floor_pct, ceil_pct = coefs

    nav_d = _decimal_or(nav, Decimal("0"))
    if nav_d <= 0 or base_pct <= 0:
        return static_val

    target_base = nav_d * base_pct

    # P&L health multiplier: -1 (worst recent P&L) → fade, +1 → no change.
    # We use a soft formula so a small recent loss doesn't slam the size.
    pnl = _decimal_or(strategy_net_pnl_recent, Decimal("0"))
    fills = _decimal_or(strategy_total_fills_recent, Decimal("0"))
    pnl_per_fill = pnl / fills if fills > 0 else Decimal("0")
    # The pnl-per-fill divided by the per-trade target gives a unitless
    # "fraction of risk" — clamp to [-1, +1] then apply weight.
    if target_base > 0:
        normalized_pnl = pnl_per_fill / target_base
    else:
        normalized_pnl = Decimal("0")
    if normalized_pnl < Decimal("-1"):
        normalized_pnl = Decimal("-1")
    if normalized_pnl > Decimal("1"):
        normalized_pnl = Decimal("1")
    pnl_health = Decimal("1") + pnl_weight * normalized_pnl

    regime_mult = _decimal_or(regime_multiplier, _NEUTRAL)
    if regime_mult <= 0:
        regime_mult = _NEUTRAL
    quarantine_mult = _decimal_or(quarantine_multiplier, _NEUTRAL)
    if quarantine_mult < 0:
        quarantine_mult = Decimal("0")
    if quarantine_mult == 0:
        return Decimal("0")

    result = target_base * pnl_health * regime_mult * quarantine_mult
    floor_d = nav_d * floor_pct
    ceil_d = nav_d * ceil_pct
    return _clamp(result, floor_d, ceil_d)


# ─────────────────────────────────────────────────────────────────────────────
# Volume-flow z-score thresholds (dynamic from realised vol)
# ─────────────────────────────────────────────────────────────────────────────


def volume_zscore_open_threshold(
    *,
    atr_pct: Any,
    static_threshold: Any | None = None,
) -> Decimal:
    """Z-score open threshold for volume_flow continuation setups.

    Static used 0.75 — fires on minor volume bumps. Dynamic widens with
    realised volatility (in a wild market a 0.75-z spike is noise; in a
    quiet market it's meaningful).
    """
    cfg = _load_dynamic_block().get("volume_zscore")
    static_val = _decimal_or(static_threshold, Decimal("0.75"))
    if not isinstance(cfg, dict):
        return static_val
    coefs = _strict_pull(cfg, "open_base", "open_atr_weight", "open_min", "open_max")
    if coefs is None:
        return static_val
    base, atr_weight, floor, ceil = coefs
    atr = _decimal_or(atr_pct, Decimal("0"))
    if atr < 0:
        atr = Decimal("0")
    return _clamp(base + atr_weight * atr, floor, ceil)


def volume_zscore_exhaust_threshold(
    *,
    atr_pct: Any,
    static_threshold: Any | None = None,
) -> Decimal:
    """Z-score exhaustion threshold (where a vol spike flips to reversal).

    Static used 3.4 — same across regimes. Dynamic widens with vol so
    in a wild market we require an even more extreme outlier.
    """
    cfg = _load_dynamic_block().get("volume_zscore")
    static_val = _decimal_or(static_threshold, Decimal("3.4"))
    if not isinstance(cfg, dict):
        return static_val
    coefs = _strict_pull(
        cfg, "exhaust_base", "exhaust_atr_weight", "exhaust_min", "exhaust_max",
    )
    if coefs is None:
        return static_val
    base, atr_weight, floor, ceil = coefs
    atr = _decimal_or(atr_pct, Decimal("0"))
    if atr < 0:
        atr = Decimal("0")
    return _clamp(base + atr_weight * atr, floor, ceil)


# ─────────────────────────────────────────────────────────────────────────────
# Min bar return — scales with ATR
# ─────────────────────────────────────────────────────────────────────────────


def min_bar_return_threshold(
    *,
    atr_pct: Any,
    static_threshold: Any | None = None,
) -> Decimal:
    """Minimum single-bar return for a setup to count, as a fraction of
    the symbol's own ATR. Static values (0.0004 / 0.0006) were one-size-
    fits-all and meaningless on a high-vol crypto. The formula gives the
    same "fraction of one ATR" gate across asset classes."""
    cfg = _load_dynamic_block().get("min_bar_return")
    static_val = _decimal_or(static_threshold, Decimal("0.0005"))
    if not isinstance(cfg, dict):
        return static_val
    coefs = _strict_pull(cfg, "atr_multiple", "min_threshold", "max_threshold")
    if coefs is None:
        return static_val
    atr_multiple, floor, ceil = coefs
    atr = _decimal_or(atr_pct, Decimal("0"))
    if atr < 0:
        atr = Decimal("0")
    return _clamp(atr_multiple * atr, floor, ceil)


# ─────────────────────────────────────────────────────────────────────────────
# Volatility acceptable band (replaces fixed atr_min / atr_max)
# ─────────────────────────────────────────────────────────────────────────────


def acceptable_atr_band(
    *,
    median_atr_pct: Any,
    static_min: Any | None = None,
    static_max: Any | None = None,
) -> tuple[Decimal, Decimal]:
    """(min, max) acceptable ATR% band, derived from the symbol's recent
    median ATR%. The static (0.004 / 0.06) ruled out half of crypto and
    most utility stocks. The formula expresses the band as multiples of
    the symbol's own typical ATR — adapts per name automatically."""
    cfg = _load_dynamic_block().get("atr_band")
    static_pair = (
        _decimal_or(static_min, Decimal("0.0001")),
        _decimal_or(static_max, Decimal("1")),
    )
    if not isinstance(cfg, dict):
        return static_pair
    coefs = _strict_pull(cfg, "lower_multiple", "upper_multiple", "abs_min", "abs_max")
    if coefs is None:
        return static_pair
    lo_mult, hi_mult, abs_floor, abs_ceil = coefs
    median = _decimal_or(median_atr_pct, Decimal("0"))
    if median <= 0:
        return (
            _decimal_or(static_min, abs_floor),
            _decimal_or(static_max, abs_ceil),
        )
    lo = max(abs_floor, lo_mult * median)
    hi = min(abs_ceil, hi_mult * median)
    if lo > hi:
        lo = abs_floor
        hi = abs_ceil
    return (lo, hi)


# ─────────────────────────────────────────────────────────────────────────────
# Event-driven shock threshold (function of rolling news-score dispersion)
# ─────────────────────────────────────────────────────────────────────────────


def event_shock_threshold(
    *,
    news_score_dispersion: Any,
    static_threshold: Any | None = None,
) -> Decimal:
    """Minimum |news_score| to count as an event-driven shock. The
    formula widens when the news-score distribution itself is wide
    (lots of noisy headlines → require a bigger signal to stand out)
    and tightens in calm news regimes (a moderate score is meaningful)."""
    cfg = _load_dynamic_block().get("event_shock")
    static_val = _decimal_or(static_threshold, Decimal("0.45"))
    if not isinstance(cfg, dict):
        return static_val
    coefs = _strict_pull(cfg, "base", "dispersion_weight", "min_threshold", "max_threshold")
    if coefs is None:
        return static_val
    base, disp_weight, floor, ceil = coefs
    disp = _decimal_or(news_score_dispersion, Decimal("0"))
    if disp < 0:
        disp = Decimal("0")
    return _clamp(base + disp_weight * disp, floor, ceil)


# ─────────────────────────────────────────────────────────────────────────────
# Pairs-trading z-score open threshold (function of cointegration health)
# ─────────────────────────────────────────────────────────────────────────────


def pairs_zscore_open_threshold(
    *,
    cointegration_health: Any,
    static_threshold: Any | None = None,
) -> Decimal:
    """Pairs-trading entry z-score. Tightens when cointegration is
    strong (a smaller stretch is a real reversion); widens when weak
    (need a bigger move to overcome relationship breakdown risk).

    ``cointegration_health`` is a [0,1] score from the pair's recent
    spread stationarity / hedge-ratio stability (caller supplies it).
    """
    cfg = _load_dynamic_block().get("pairs_zscore")
    static_val = _decimal_or(static_threshold, Decimal("2.0"))
    if not isinstance(cfg, dict):
        return static_val
    coefs = _strict_pull(cfg, "base", "health_weight", "min_threshold", "max_threshold")
    if coefs is None:
        return static_val
    base, health_weight, floor, ceil = coefs
    health = _decimal_or(cointegration_health, _FEATURE_MIDPOINT)
    # Higher health → tighter (lower) threshold.
    delta = health_weight * (health - _FEATURE_MIDPOINT)
    return _clamp(base - delta, floor, ceil)


# ─────────────────────────────────────────────────────────────────────────────
# Regime-rotation score trigger
# ─────────────────────────────────────────────────────────────────────────────


def regime_rotation_score_trigger(
    *,
    market_state_score: Any,
    static_threshold: Any | None = None,
) -> Decimal:
    """Demand-score threshold for regime_rotation to fire. Lowers when
    market_state_score is clear (regime is well-defined, rotation is
    confident); raises when score is near zero (regime mixed, require
    a stronger demand signal before rotating)."""
    cfg = _load_dynamic_block().get("regime_rotation")
    static_val = _decimal_or(static_threshold, Decimal("0.35"))
    if not isinstance(cfg, dict):
        return static_val
    coefs = _strict_pull(cfg, "base", "clarity_weight", "min_threshold", "max_threshold")
    if coefs is None:
        return static_val
    base, clarity_weight, floor, ceil = coefs
    score_abs = abs(_decimal_or(market_state_score, Decimal("0")))
    return _clamp(base - clarity_weight * score_abs, floor, ceil)


# ─────────────────────────────────────────────────────────────────────────────
# Anti-churn cooldown (function of regime + recent fill density)
# ─────────────────────────────────────────────────────────────────────────────


def anti_churn_cooldown_sec(
    *,
    mode: str,
    market_state_score: Any = 0,
    recent_fill_rate_per_min: Any = 0,
    static_cooldown: Any | None = None,
) -> Decimal:
    """Post-fill cooldown (seconds) before the same (broker, symbol) can
    re-trade. Static values were 120/180/600 per mode. The dynamic
    formula starts from a mode-specific base and extends with two
    signals:
      * |market_state_score| — in strong regimes shorter cooldowns
        (catch the trend) ; in mixed regimes longer (avoid churn).
      * recent_fill_rate_per_min — when the symbol is firing rapidly,
        extend cooldown to dampen overtrading.
    """
    cfg = _load_dynamic_block().get("anti_churn_cooldown")
    static_val = _decimal_or(static_cooldown, Decimal("180"))
    if not isinstance(cfg, dict):
        return static_val
    bases = cfg.get("base_by_mode") or {}
    if not isinstance(bases, dict):
        return static_val
    # Try mode-specific base; fall back to base_default. Both come from
    # YAML strictly — no code-baked default.
    base = _strict_decimal(bases.get(str(mode).strip().lower()))
    if base is None:
        base = _strict_decimal(cfg.get("base_default"))
    if base is None:
        return static_val
    coefs = _strict_pull(cfg, "clarity_weight", "fill_rate_weight", "min_sec", "max_sec")
    if coefs is None:
        return static_val
    clarity_weight, fill_rate_weight, floor, ceil = coefs
    score_abs = abs(_decimal_or(market_state_score, Decimal("0")))
    rate = _decimal_or(recent_fill_rate_per_min, Decimal("0"))
    if rate < 0:
        rate = Decimal("0")
    adjustment = (-clarity_weight * score_abs) + (fill_rate_weight * rate)
    return _clamp(base + adjustment, floor, ceil)
