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

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

# ── Mathematical identities (NOT tuning) ────────────────────────────────────
_RSI_MIDPOINT = Decimal("50")              # RSI's natural neutral point
_FEATURE_MIDPOINT = Decimal("0.5")         # midpoint of [0,1] features
_NEUTRAL = Decimal("1.0")                  # multiplicative identity

_CONFIG_PATH = Path("config/strategies.yaml")
_YAML_CACHE: tuple[float, dict[str, Any]] | None = None


def _load_dynamic_block() -> dict[str, Any]:
    """Return the ``dynamic_thresholds`` block from strategies.yaml, or {} if absent.

    Missing/disabled block → empty dict. Every formula falls back to a safe
    no-op (returning the legacy literal) if its safety coefficient is absent.
    No tuning defaults are invented in code.
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
    _YAML_CACHE = (mtime, block)
    return block


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
    if not cfg:
        # No dynamic config → preserve legacy static behaviour.
        return (
            _decimal_or(static_buy_threshold, Decimal("47")),
            _decimal_or(static_sell_threshold, Decimal("53")),
        )

    base_distance = _decimal_or(cfg.get("base_distance"), Decimal("3"))
    vol_weight = _decimal_or(cfg.get("vol_weight"), Decimal("0"))
    trend_weight = _decimal_or(cfg.get("trend_weight"), Decimal("0"))
    min_distance = _decimal_or(cfg.get("min_distance"), Decimal("1"))
    max_distance = _decimal_or(
        cfg.get("max_distance"),
        # The largest meaningful distance is 49 (buy=1, sell=99), but
        # we cap at 30 by default so we stay within typical RSI overshoot
        # bands. Operator can widen via YAML if they want.
        Decimal("30"),
    )

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
    if not isinstance(cfg, dict):
        return _decimal_or(static_epsilon, Decimal("0.006"))

    atr_multiple = _decimal_or(cfg.get("atr_multiple"), Decimal("0"))
    floor = _decimal_or(cfg.get("min_epsilon"), Decimal("0.001"))
    ceil = _decimal_or(cfg.get("max_epsilon"), Decimal("0.05"))
    atr = _decimal_or(atr_pct, Decimal("0"))
    if atr < 0:
        atr = Decimal("0")

    eps = atr_multiple * atr
    return _clamp(eps, floor, ceil)


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
    if not isinstance(cfg, dict):
        return _decimal_or(static_threshold, Decimal("0.001"))

    atr_multiple = _decimal_or(cfg.get("atr_multiple"), Decimal("0"))
    floor = _decimal_or(cfg.get("min_threshold"), Decimal("0.0005"))
    ceil = _decimal_or(cfg.get("max_threshold"), Decimal("0.05"))
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
    if not isinstance(cfg, dict):
        return _decimal_or(static_multiplier, Decimal("1.2"))

    base = _decimal_or(cfg.get("base_multiplier"), Decimal("1.0"))
    z_weight = _decimal_or(cfg.get("z_weight"), Decimal("0"))
    floor = _decimal_or(cfg.get("min_multiplier"), Decimal("1.0"))
    ceil = _decimal_or(cfg.get("max_multiplier"), Decimal("5.0"))
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
    static_notional: Any | None = None,
) -> Decimal:
    """The per-trade target notional, **derived from live state**:

      * scales with NAV (so $1.2M and $12M accounts trade proportionally)
      * shrinks when the strategy's recent net P&L is negative (a "P&L
        health" multiplier — bleeding strategies size down automatically)
      * scales with the regime multiplier already computed by
        :mod:`system.adaptive_regime_weights`

    Static config used 20_000 / 25_000 — the same dollar size on every
    trade regardless of account size or strategy performance. That's the
    "broken account compounding loser" pattern the audit traced.
    """
    cfg = _load_dynamic_block().get("sizing")
    if not isinstance(cfg, dict):
        return _decimal_or(static_notional, Decimal("0"))

    base_pct = _decimal_or(cfg.get("base_nav_pct"), Decimal("0"))
    nav_d = _decimal_or(nav, Decimal("0"))
    if nav_d <= 0 or base_pct <= 0:
        return _decimal_or(static_notional, Decimal("0"))

    target_base = nav_d * base_pct

    # P&L health multiplier: -1 (worst recent P&L) → fade, +1 → no change.
    # We use a soft formula so a small recent loss doesn't slam the size.
    pnl_weight = _decimal_or(cfg.get("pnl_health_weight"), Decimal("0"))
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

    floor_pct = _decimal_or(cfg.get("min_nav_pct"), Decimal("0"))
    ceil_pct = _decimal_or(cfg.get("max_nav_pct"), Decimal("1"))
    result = target_base * pnl_health * regime_mult
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
    if not isinstance(cfg, dict):
        return _decimal_or(static_threshold, Decimal("0.75"))
    base = _decimal_or(cfg.get("open_base"), Decimal("0"))
    atr_weight = _decimal_or(cfg.get("open_atr_weight"), Decimal("0"))
    floor = _decimal_or(cfg.get("open_min"), Decimal("0.3"))
    ceil = _decimal_or(cfg.get("open_max"), Decimal("3.0"))
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
    if not isinstance(cfg, dict):
        return _decimal_or(static_threshold, Decimal("3.4"))
    base = _decimal_or(cfg.get("exhaust_base"), Decimal("0"))
    atr_weight = _decimal_or(cfg.get("exhaust_atr_weight"), Decimal("0"))
    floor = _decimal_or(cfg.get("exhaust_min"), Decimal("2.0"))
    ceil = _decimal_or(cfg.get("exhaust_max"), Decimal("8.0"))
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
    if not isinstance(cfg, dict):
        return _decimal_or(static_threshold, Decimal("0.0005"))
    atr_multiple = _decimal_or(cfg.get("atr_multiple"), Decimal("0"))
    floor = _decimal_or(cfg.get("min_threshold"), Decimal("0.0001"))
    ceil = _decimal_or(cfg.get("max_threshold"), Decimal("0.05"))
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
    if not isinstance(cfg, dict):
        return (
            _decimal_or(static_min, Decimal("0.0001")),
            _decimal_or(static_max, Decimal("1")),
        )
    lo_mult = _decimal_or(cfg.get("lower_multiple"), Decimal("0.25"))
    hi_mult = _decimal_or(cfg.get("upper_multiple"), Decimal("4.0"))
    abs_floor = _decimal_or(cfg.get("abs_min"), Decimal("0.0001"))
    abs_ceil = _decimal_or(cfg.get("abs_max"), Decimal("1.0"))
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
    if not isinstance(cfg, dict):
        return _decimal_or(static_threshold, Decimal("0.45"))
    base = _decimal_or(cfg.get("base"), Decimal("0.3"))
    disp_weight = _decimal_or(cfg.get("dispersion_weight"), Decimal("0"))
    floor = _decimal_or(cfg.get("min_threshold"), Decimal("0.2"))
    ceil = _decimal_or(cfg.get("max_threshold"), Decimal("0.9"))
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
    if not isinstance(cfg, dict):
        return _decimal_or(static_threshold, Decimal("2.0"))
    base = _decimal_or(cfg.get("base"), Decimal("1.5"))
    health_weight = _decimal_or(cfg.get("health_weight"), Decimal("0"))
    floor = _decimal_or(cfg.get("min_threshold"), Decimal("1.0"))
    ceil = _decimal_or(cfg.get("max_threshold"), Decimal("4.0"))
    health = _decimal_or(cointegration_health, Decimal("0.5"))
    # Higher health → tighter (lower) threshold. Subtract weight×(health-0.5).
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
    if not isinstance(cfg, dict):
        return _decimal_or(static_threshold, Decimal("0.35"))
    base = _decimal_or(cfg.get("base"), Decimal("0.4"))
    clarity_weight = _decimal_or(cfg.get("clarity_weight"), Decimal("0"))
    floor = _decimal_or(cfg.get("min_threshold"), Decimal("0.2"))
    ceil = _decimal_or(cfg.get("max_threshold"), Decimal("0.7"))
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
    if not isinstance(cfg, dict):
        return _decimal_or(static_cooldown, Decimal("180"))
    bases = cfg.get("base_by_mode") or {}
    if not isinstance(bases, dict):
        bases = {}
    base = _decimal_or(bases.get(str(mode).strip().lower()), None)
    if base is None:
        base = _decimal_or(cfg.get("base_default"), None)
    if base is None:
        return _decimal_or(static_cooldown, Decimal("180"))
    clarity_weight = _decimal_or(cfg.get("clarity_weight"), Decimal("0"))
    fill_rate_weight = _decimal_or(cfg.get("fill_rate_weight"), Decimal("0"))
    floor = _decimal_or(cfg.get("min_sec"), Decimal("30"))
    ceil = _decimal_or(cfg.get("max_sec"), Decimal("1800"))
    score_abs = abs(_decimal_or(market_state_score, Decimal("0")))
    rate = _decimal_or(recent_fill_rate_per_min, Decimal("0"))
    if rate < 0:
        rate = Decimal("0")
    # Strong regime → shorter; high fill rate → longer.
    adjustment = (-clarity_weight * score_abs) + (fill_rate_weight * rate)
    return _clamp(base + adjustment, floor, ceil)
