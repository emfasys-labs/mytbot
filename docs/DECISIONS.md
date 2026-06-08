# DECISIONS.md
# =============
# Every significant architectural decision, with reasoning.
# Add to this file whenever a decision is made.
# This file keeps Claude, Cursor, and the developer aligned.

**Hygiene note:** A later block reuses labels **D012–D014** for funding / coordination topics while **D012–D014** already appear as local-first AI / tier gating. When implementing, read the **heading title and date**, not the number alone. Renumbering is a planned doc cleanup.

| Reused id | Earlier section | Later section (lower in file) |
|-----------|-----------------|----------------------------------|
| D012 | Local-first AI architecture | Funding rate arbitrage as first arbitrage strategy |
| D013 | Dual-model ensemble consensus | Strategy coordination layer above strategy outputs |
| D014 | Materiality-based tier gating | Global edge coordinator vs D015-as-final allocator |

---

## D157 — Strategy edge gate (prove post-cost expectancy before capital)
**Date:** 2026-06-06
**Decision:** A strategy receives capital only after demonstrating positive out-of-sample expectancy AFTER COSTS. `backtest/edge_gate.py` aggregates the existing walk-forward harness across the universe into per-strategy metrics (out-of-sample windows, total trades, expectancy/trade, consistency = fraction of profitable windows, profit factor) and `decide_verdict()` emits `allowed` (full size) / `reduced` (half) / `blocked` (zero) / `insufficient_data` (→ `unproven_policy`). Verdicts persist to an atomic JSON registry (`data/state/edge_gate_verdicts.json`) written by `scripts/run_edge_gate.py` (run on a schedule). Enforcement lives in `system/trading_loop/loop.py::_apply_edge_gate_filter`: `blocked` strategies' candidates are dropped; non-blocked candidates have their confidence scaled by the verdict's size multiplier so the down-weight propagates to every downstream path (sizing + the D156 orchestrator netting). The verdict is the *a-priori* trust; the D156 orchestrator's recent-P&L trust is the *posterior*. **Gated OFF** by `config/strategies.yaml::edge_gate.enabled` (default false). `unproven_policy` (default `reduce`) decides cold-start treatment of un-backtestable strategies (news/cross-sectional) and thin samples — `reduce` (half size, won't halt trading) or `block` (strict). Only ever reduces/removes capital; never bypasses risk. The edge gate's cost model is the backtest `fee_bps`/`slippage_bps` — set to realistic LIVE costs.
**Reason:** The D156 diagnosis proved the base strategies are near-breakeven; nothing stopped unproven strategies deploying capital. Running the gate on live data surfaced a deeper problem: the feature store holds only **~135 hourly bars/symbol across 23 symbols** (≈5 days), and over that window momentum/mean-reversion/volatility generated **zero** completed backtest round-trips while volume_flow made 2 (both losing). So **no strategy can currently demonstrate edge at all** — the live churn is machinery-driven (allocator/coordinator), not edge-driven. The gate correctly returns `insufficient_data` for every strategy, which under the default `reduce` policy halves their size until proven. This makes the unprovability explicit and bounded instead of silently risking full capital on unvalidated signals. The real remediation is upstream: backfill far more history (the gate adapts window sizes to available data but cannot manufacture a sample).
**Status:** Implemented. New: `backtest/edge_gate.py` (pure `decide_verdict` + `aggregate_walk_forward` + atomic `EdgeGateRegistry`), `scripts/run_edge_gate.py` (adaptive-window CLI, evaluates the 4 OHLCV strategies; `--dry-run`/`--all`/`--symbols`/`--min-bars`), `config/strategies.yaml::edge_gate`, loop enforcement (`_load_edge_gate` mtime-cached + `_apply_edge_gate_filter`). Tests: `tests/test_edge_gate.py` (13), `tests/test_edge_gate_loop_filter.py` (4). Full suite 1836 passed, 3 skipped.

**D157.1 — Backfill + first real verdicts (2026-06-06):** Ran the existing pipeline backfill (`python run_pipeline.py --backfill` for 2y daily, `--training-backfill --symbols ...` for 730d hourly) — the feature store went from ~5 days to **500–730 daily bars and 13k–17k hourly bars per symbol** across the 23-symbol universe (`feature_snapshots`: 12.5k 1d rows + 40k→deep 1h rows). Re-running the gate produced the first real verdicts and a **decisive, timeframe-dependent result**: on **1d**, `mean_reversion` is the one proven strategy (36 trades, +$3,719, expectancy +$103/trade, PF 2.30, 57% win → ALLOWED); momentum doesn't fire on daily at all. On **1h** (the timeframe the live loop actually trades), the picture inverts and is brutal: `mean_reversion` **BLOCKED** (4,513 trades, **−$347,532**, expectancy −$77/trade, PF 0.43, 32% win), `volume_flow` BLOCKED (−$6,618, PF 0.79), `volatility_regime` BLOCKED (−$33,437, PF 0.25, 13.6% win), momentum fires 0 trades. **This is the root cause of the live bleed: the system trades 1h, where none of its strategies have edge and mean-reversion is actively destructive — yet mean-reversion has genuine edge on the daily timeframe it is NOT being run on.** Key safety rule established: the canonical verdict registry MUST be generated on the live trading timeframe (a 1d verdict applied to a 1h book would have ALLOWED the −$347k strategy). The canonical `data/state/edge_gate_verdicts.json` is now the 1h run. **Next (decision needed): either (a) move mean-reversion to the daily timeframe where it is proven, (b) re-tune/replace the 1h strategies, or (c) enable the gate on 1h verdicts to halt the bleeders until a strategy proves 1h edge.**

---

## D158 — Forge + validate trend weapons (sniper/shotgun); confirm edge is daily-only
**Date:** 2026-06-07
**Decision:** Added two new daily-horizon, direction-agnostic trend strategies to expand the arsenal beyond mean-reversion: `strategies/trend_breakout.py` (Donchian/Turtle channel breakout — "sniper", rides large moves) and `strategies/trend_following.py` (fast/slow MA time-series momentum — "shotgun"). Both emit `target_notional` sizing metadata and a `weapon_class` tag, are gated OFF in config pending proof, and are wired into the edge gate (`scripts/run_edge_gate.py` `_PER_SYMBOL_STRATEGIES`, plus a new `--only` strategy filter and a force-enable so gated-off strategies can still be evaluated). Ran the edge gate across the backfilled universe on both timeframes.
**Result (decisive, confirms D157.1):** On **1d** the new trend weapons are the strongest in the book — `trend_breakout` **ALLOWED** (162 trades, +$54,816, PF **2.78**, 64% win), `trend_following` **REDUCED** (552 trades, +$84,586, PF 1.99, just under the 0.55 consistency bar). Combined with mean_reversion (ALLOWED, PF 1.80) and momentum_breakout (now ALLOWED with more history, PF 1.33), there are **four proven daily edges — and they are complementary** (trend weapons ride moves, mean-reversion fades them → low/negative correlation, the diversification foundation). On **1h**, every weapon is BLOCKED, including the new trend ones (`trend_breakout` PF 0.61/33% win, `trend_following` PF 0.51/30% win) — the same profitable-on-daily / catastrophic-on-1h inversion seen for mean-reversion. **CONCLUSION: across six different weapon types, there is no positive-expectancy edge at the 1h horizon after realistic costs — the signal-to-noise at hourly resolution does not clear ~30bps round-trip. All proven edge lives at the daily horizon. There is no viable "fast hunter" with these weapons + retail data; a real 1h/knife edge would require microstructure/order-flow signals and far better-than-yfinance data.**
**Reason:** The user's "army of heterogeneous hunters" vision needs an arsenal of uncorrelated, proven weapons. This forged two strong ones and proved (not assumed) that the achievable edge is daily, not intraday. The aggregate-activity the user wants ("thousands of hunters → frequent shots") is still attainable: 4 weapons × a large universe fire often in aggregate even though each hunter is daily-paced.
**Status:** Implemented. `strategies/trend_breakout.py`, `strategies/trend_following.py`, `config/strategies.yaml` (both gated off + edge_gate timeframe warning), `scripts/run_edge_gate.py` (`--only`, force-enable). Daily verdicts → `data/state/edge_gate_verdicts_1d.json` (4 allowed/reduced). Tests: `tests/test_trend_strategies.py` (9). Full suite 1845 passed, 3 skipped.

**D158 Phase 1 — Move the army to daily (2026-06-07):** Switched the live system to the daily horizon where the proven edge lives. Changes (all config/wiring, reversible): (1) `system/orchestrator.py` `TIMEFRAME` default `1h→1d`; (2) `config/data_pipeline.yaml::incremental` interval `1h→1d`, period `5d→1mo`; (3) `config/strategies.yaml` — **enabled** the 4 proven weapons (trend_breakout, trend_following, mean_reversion, momentum_breakout), **parked** (enabled:false) the 5 unproven/no-edge ones (volume_flow=insufficient sample, volatility_regime=no edge, event_driven_news/pairs_trading/regime_rotation=not validatable per-symbol); (4) turned **ON** `edge_gate.enabled` and `portfolio_orchestrator.enabled`; (5) canonical `data/state/edge_gate_verdicts.json` now = the daily run (matches live timeframe — the safety rule). Wired the two new weapons into the loop: imports + instantiation + `strategies` dict + `collect_raw_signals_for_symbol` (added `trend_breakout`/`trend_following` params, backward-compatible defaults, + a direction-agnostic generate block) + both call sites. End-to-end smoke test confirms: enabled flags correct, trend weapons reach the candidate pipeline, gate enabled + blocks volatility_regime + allows trend_breakout, orchestrator enabled. Full suite 1845 passed. **Next (Phase 2): redefine the three risk modes as global-throttle × per-weapon-temperament. Then Phase 3: expand weekly/monthly snipers. To revert Phase 1: set TIMEFRAME=1h + incremental interval 1h + flip the enabled flags + edge_gate/orchestrator off.**

---

## D157.2 — 1h mean-reversion re-tune attempt (conclusive: not salvageable on 1h)
**Date:** 2026-06-06
**Decision:** Attempted to re-tune mean_reversion for the live 1h timeframe rather than move it to daily. Fixed two harness bugs that made history-dependent tuning impossible: (1) `backtest/harness.py` walk-forward discarded the train window and passed only the 63-bar test slice to the strategy, so trend MAs / long lookbacks had no history — now it passes train+test as warmup *context* via a new `warmup_bars` param and only counts trades in the test region (backward-compatible, default 0); (2) the D141 dynamic-threshold block overrode static RSI, so the tuner (`scripts/tune_mean_reversion.py`) monkeypatches it off to compare static configs. Added a config-gated `trend_filter` to mean_reversion (default off; only fade *with* the longer trend). Swept three lever families on 730d of 1h data across 23 symbols at realistic costs (10bps fee + 5bps slippage): **(a) RSI extremity** 47/53→25/75 cut the bleed ~1400× (−$839k → −$81k) but never positive; **(b) trend filter** (MA100/200) slashed trades to ~78 and PF to 0.57 but still −$4,426; **(c) exit policy** (take-profit at mean, fixed TP/SL, short max-hold) — every variant negative, and the canonical mean-reversion exits made it *worse* (exit-at-mean PF 0.28, max_hold-3 PF 0.09/4.8% win). **Conclusion: there is no positive-expectancy mean_reversion config on 1h after costs — the signal does not reliably revert on hourly bars, so you stop out of noise and pay ~30bps round-trip on coin-flips. Its genuine edge is on the daily timeframe (D157.1: PF 2.30).**
**Reason:** The user elected to exhaust the 1h tuning levers before accepting a timeframe change. The sweeps make the verdict robust and evidence-based rather than assumed. The harness warmup fix and the config-gated trend filter are kept (real improvements: the trend filter alone reduces the live 1h bleed ~190×, useful as a defensive default while the edge gate blocks the strategy).
**Status:** Implemented. `backtest/harness.py` (`warmup_bars`), `strategies/mean_reversion.py` (`trend_filter` block, default off), `scripts/tune_mean_reversion.py` (entry + exit sweeps). Full suite 1836 passed, 3 skipped. **Recommendation: run mean_reversion on the daily timeframe where it is proven; keep it edge-gate-blocked on 1h.**

---

## D156 — Portfolio netting orchestrator (stop strategies cancelling each other)
**Date:** 2026-06-06
**Decision:** Add a portfolio-construction layer (`portfolio/portfolio_orchestrator.py`) between strategy alphas and the risk/execution path. It (1) **nets** every strategy's directional intent for a symbol into ONE conviction-weighted target — opposing strategies reduce conviction instead of opening an offsetting long+short pair; (2) **sizes by conviction** (concentration exponent, per-name cap) not equal-weight, scaled to a mode-dependent gross budget with deliberate **net management** (|net| ≤ `net_cap_pct_of_gross` × gross when two-sided; a one-sided high-conviction book is left fully directional); (3) **protects maturing edge** — a profitable or <30-min-old position is not flipped/closed unless an opposing conviction clears a hard bar, and sub-band diffs are suppressed. When enabled, the loop routes the netted minimal order set through the existing `process_coordinator_action` → `_process_signal_global` (RiskEngine → ExecutionEngine) path unchanged, and **skips the global-edge rotation/recycle tick** (the orchestrator now owns rebalancing). Per-strategy *trust* weights derived from recent FillLog P&L down-weight persistently-bleeding layers. **Gated OFF** by `config/strategies.yaml::portfolio_orchestrator.enabled` (default false) → zero behaviour change until flipped.
**Reason:** Diagnostic (`scripts/diagnose_strategy_interaction.py`, run 2026-06-06) on the live book proved the no-growth bleed was structural, not a sign inversion or fees: 7 directional strategies (several structurally opposed — momentum buys breakouts, mean-reversion buys dips) produced an accidental market-neutral basket (gross ~103%, **net only 13.5%** of gross, 45 equal-weighted ~$60k names, 18 symbols with strategies on opposite sides), so the book captured no market direction. Worse, the management layers were the realised bleed: `global_edge_rotation` closed **10 positions at a 0% win rate** and `capital_recycle` −$733 (0% win) — force-closing maturing positions (median hold 71 min) to chase marginally-better opportunities — while the alpha that was allowed to mature was positive (`profit_harvest_monitor` +$605/73% win, `volume_flow` +$85/67%). The pre-existing `anti_churn` gate only blocked *same-symbol* contradictions; `allocation_engine` set `net_exposure_target = gross`. No layer maximized the *combined* net effect of all strategies — the user's requirement. This adds it.
**Status:** Implemented. New: `portfolio/portfolio_orchestrator.py` (pure Decimal function + `OrchestratorConfig.from_yaml` + `build_intents_from_candidates`/`build_intents_from_raw_signals` adapters), `config/strategies.yaml::portfolio_orchestrator`, `system/trading_loop/loop.py::_run_orchestrated_tick` + `_orchestrator_strategy_trust` + gated branch, `scripts/diagnose_strategy_interaction.py` (read-only diagnostic). Tests: `tests/test_portfolio_orchestrator.py` (17). Full suite 1819 passed, 3 skipped. **All strategies stay enabled; not all apply every tick — resolved at the portfolio level.** Risk engine retains unconditional veto (rule 2 preserved). Next: paper-validate with `enabled: true`, then compare net exposure / realised curve vs the gated-off baseline.

---

## D132 — Bleed-stopper safety rebuild for fully deployed trading
**Date:** 2026-05-23
**Decision:** The capital slider is a deployment target only; it may not erase safety dampers. `gross_exposure.unleash` is now config-governed and only relaxes opportunity-shape pressure, while drawdown throttle, execution quality, volatility overlays, and Wave 8 overlays remain live at `capital_pct=1.0`. Intraday derisk can activate a persisted risk-engine open lock that blocks fresh opens while allowing reduce-only exits and hedges. Wave 9 no longer exempts top-ups; genuine top-ups receive a scaled-down cost cushion, and D125 single-name clamps no longer stamp `sizing_topup_existing`. Paper mode now enforces execution pre-checks instead of filling orders that would fail live spread/liquidity/slippage gates. Confidence and trade-quality thresholds now adapt to market state and rolling win-rate inside hard circuit-breaker bands. FX, equity-index, and crypto cluster caps use configured base caps scaled by market-state quality.
**Reason:** A fully deployed paper/live system was able to keep seeking new risk while intraday derisk was firing, and `capital_pct=1.0` previously interpolated multiple safety dampers to `1.0`. That made “max deployment” behave like “ignore dampers,” creating churn and loss leakage.
**Status:** Implemented in `portfolio/allocation_engine.py`, `risk/engine.py`, `risk/drawdown_governor.py`, `system/orchestrator.py`, `execution/engine.py`, `config/allocation.yaml`, and `config/risk_limits.yaml`.

---

## D067 — Fee-first execution gating and accounting
**Date:** 2026-05-01
**Decision:** Transaction costs are treated as mandatory execution constraints across all strategy flows. The Wave 9 pre-flight cost gate is enabled by default, and per-fill fees are always persisted into daily P&L accumulation (including opening/add flows, not only realised closes).
**Reason:** Frequent churn can harvest gross unrealised moves while destroying net returns after commissions/fees/spread/slippage. Cost-awareness must be universal and strategy-agnostic at the execution boundary.
**Status:** Implemented in `config/execution_models.yaml`, `system/trading_loop/loop.py`, and `run_m5.py`.

---

## D001 — Adapter pattern for all brokers
**Date:** 2026-04-04
**Decision:** Every broker implements a single abstract interface (`brokers/base.py`).
The rest of the system only speaks this interface, never broker-specific code.
**Reason:** Adding a new exchange should require zero changes to strategy, risk, or execution code.
Bybit, Deribit, OKX, or any future exchange can be added with one new file.
**Status:** Implemented in M1.

---

## D002 — IBKR as primary broker
**Date:** 2026-04-04
**Decision:** Interactive Brokers Pro is the primary broker for all non-crypto assets.
**Reason:** Only single platform that covers US stocks, UK stocks, bonds, ETFs, forex, options,
futures, and now 11 crypto assets. Full API access. Used by professional firms.
**Status:** Primary non-crypto venue; IBKR adapter implemented (`brokers/ibkr/`). Account setup is operational (owner).

---

## D003 — Kraken + Binance as crypto layer
**Date:** 2026-04-04
**Decision:** Kraken is primary crypto exchange, Binance is secondary for liquidity/coverage.
**Reason:** IBKR crypto covers only 11 coins. Kraken adds 640+ pairs, GBP-native, UK-friendly.
Binance adds highest liquidity and widest coin selection.
**Status:** Adapters implemented; live use requires valid API keys in `.env`.

---

## D004 — Risk engine has unconditional veto power
**Date:** 2026-04-04
**Decision:** No order can be placed without passing through the risk engine.
No bypass, no flag, no override in code.
**Reason:** The single most dangerous failure mode is an automated system placing
orders the human would not have approved. Risk engine is the last line of defence.
**Status:** Skeleton implemented in `risk/engine.py`.

**Amendment (D015, 2026-04-11):** The D015 allocator may be the single source of truth for *sizing* and portfolio-level exposure targets. The risk engine still evaluates every order and may veto for kill switch, drawdown/daily loss, min order, M8 micro-live guards (when active), proportionality, confidence floor, asset-class limits, cooldown, and operational integrity — but when `allocator_d015_primary` is set (default unless `ALLOCATOR_D015_LEGACY_FALLBACK=true`), it does **not** re-apply duplicate caps that the allocator already encodes (`max_gross_exposure_pct`, `max_position_pct`, catalyst/quality/theme checks from `risk_modes.yaml`). Mode labels from `risk_modes.yaml` still apply to the risk config for display; numeric mode overlays are skipped in primary mode.

---

## D005 — AI advises, rules execute
**Date:** 2026-04-04
**Decision:** AI/LLM components are used for news classification, sentiment scoring,
and trade rationale generation only. They never have direct access to order placement.
**Reason:** LLMs are not deterministic and cannot be audited the same way rule-based
systems can. AI output is a score that feeds into the signal engine (and optionally
the signal accumulator), which feeds into the risk engine. Every trade must have a
traceable, auditable decision path.
**Status:** Implemented (M6); **superseded in provider choice** by **D012** (local-first routing). Invariant unchanged: **AI never executes orders.**

---

## D006 — Paper mode before live, always
**Date:** 2026-04-04
**Decision:** Every strategy runs minimum 2 weeks in paper mode before any real capital.
Paper mode is the default. Live mode requires explicit `APP_ENV=live` in `.env`.
**Reason:** Live trading behaviour differs from paper in ways that only become visible
over time. Operational failures (connectivity, reconciliation, error handling) must
be discovered in paper mode, not with real money.
**Status:** Enforced via `paper_mode` flag on all adapters.

---

## D007 — Decimal for all monetary values
**Date:** 2026-04-04
**Decision:** All prices, quantities, fees, and P&L use `Decimal`, never `float`.
**Reason:** Float arithmetic introduces rounding errors that compound over thousands
of trades. In financial systems this is unacceptable.
**Status:** Enforced in `brokers/base.py` data models.

---

## D008 — TimescaleDB for time-series data
**Date:** 2026-04-04
**Decision:** TimescaleDB (PostgreSQL extension) for all OHLCV and tick data.
**Reason:** Standard PostgreSQL is slow on time-series queries (rolling windows,
range queries). TimescaleDB is purpose-built for this and is fully compatible
with SQLAlchemy and the rest of the PostgreSQL ecosystem.
**Status:** In docker-compose.yml. Schema in `storage/models.py`.

---

## D009 — Momentum breakout as first strategy
**Date:** 2026-04-04
**Decision:** First strategy to implement is momentum breakout on liquid assets.
**Reason:** Most debuggable — every signal has a clear, human-readable reason.
Parameters are intuitive. Backtesting is straightforward. Good first strategy
to validate the full Signal → Risk → Execution pipeline.
**Status:** Implemented in `strategies/momentum.py`. Needs live data to test.

---

## D010 — Smart order routing prefers IBKR for non-crypto
**Date:** 2026-04-04
**Decision:** Smart order router defaults to IBKR for equities, bonds, ETFs, forex.
Routes to Kraken/Binance for crypto not available on IBKR.
**Reason:** IBKR has regulatory clarity, superior execution for traditional assets,
and lowest effective cost for equities ($0.005/share vs % fees on crypto exchanges).
**Status:** Implemented in `execution/router.py`.

---

## D011 — M2 feature store table + yfinance research feed
**Date:** 2026-04-05
**Decision:** Store OHLCV plus JSON feature payloads in `feature_snapshots` (unique on symbol, timeframe, bar timestamp). Ingest historical and incremental bars via yfinance into Postgres; NewsAPI and FRED are optional parallel feeds with dedupe (headline hash) and macro upsert.
**Reason:** Single queryable store for backtests and live features; yfinance is sufficient for milestone research data before paid market-data vendors. Validation metadata attaches to the latest bar per ingest batch to limit row bloat.
**Status:** Implemented in M2 (`storage/models.py`, `data/`, `run_pipeline.py`).

---

## D012 — Local-first AI architecture (rules + FinBERT + local LLM)
**Date:** 2026-04-11
**Decision:** Replace Claude-first AI layer with local-first provider chain:
rules → FinBERT → local LLM (Ollama) → optional premium fallback (Claude, disabled by default).
No hard daily API call caps. Escalation is necessity-based using materiality, ambiguity,
novelty, and provider disagreement scores. Thresholds are starting heuristics that should
evolve into dynamic parameters via ParameterManager with regime/exposure overrides.
**Reason:** Claude API cost was economically irrational at current scale (~£20 spend for ~£20 profit).
The AI tasks (headline sentiment, event classification, rationale) are structured classification
problems that do not require frontier-model intelligence. FinBERT is purpose-built for financial
sentiment. Local LLMs (Llama, Gemma, Qwen) handle nuance. This eliminates recurring API cost,
reduces latency, improves resilience, and removes vendor lock-in — while keeping Claude available
as an escalation path for genuinely ambiguous or high-impact events.
**Status:** Implemented. Provider architecture in `ai/providers/`, router in `ai/router.py`,
escalation engine in `ai/escalation.py`, config in `config/ai.yaml`.

---

## D013 — Dual-model ensemble consensus for LLM escalation
**Date:** 2026-04-11
**Decision:** When rules + FinBERT are insufficient and local LLM escalation triggers,
run BOTH Qwen 2.5:7b and Llama 3.1:8b in parallel on the same headline. Compare results:
- **Agree** (same direction, both confident): accept with boosted confidence, skip premium.
- **Soft disagree** (same direction, weak): average scores, accept locally.
- **Hard disagree** (opposite directions): this IS the complexity signal — escalate to premium.
LLM disagreement is now 25% of the premium escalation score, making it the strongest single factor.
**Reason:** The user's insight: "how does the system know a task is complex if it's not powerful
enough to understand it?" Two independent architectures (Alibaba Qwen vs Meta Llama) trained on
different data disagreeing is a far more reliable complexity signal than any single model's
self-reported confidence. Agreement between independent models is also more trustworthy than
any single model's high confidence. This turns the fallback model from a crash-only backup
into an active participant in quality control.
**Status:** Implemented in `ai/router.py` (Phase 4 ensemble), `ai/escalation.py`
(`evaluate_ensemble`), `ai/schemas.py` (`EnsembleVerdict`), `config/ai.yaml` (ensemble settings).

---

## D014 — Materiality-based tier gating (GPU-optimized)
**Date:** 2026-04-11
**Decision:** Replace confidence-only LLM escalation gate with materiality-aware routing:
- HIGH materiality (macro, geopolitical, M&A): ALWAYS escalate to LLM ensemble,
  regardless of FinBERT confidence. FinBERT is a 110M-param model that only does
  surface sentiment — it should never make final decisions on portfolio-moving events.
- MEDIUM materiality (earnings, regulatory, crypto): escalate if FinBERT confidence < 0.75.
- LOW materiality (other, sector, company): escalate only if FinBERT confidence < 0.55.
Materiality is classified by the rules engine using a configurable event_type-to-materiality
map. GPU concurrency raised to 8 (from 3) and timeout reduced to 15s (from 60s) for
RTX 5080 deployment.
**Reason:** With GPU inference (2-3s per headline vs 60-90s on CPU), there is no longer a
performance reason to skip the LLM ensemble on material headlines. FinBERT remains valuable
as a fast pre-filter for noise (~40% of headlines) and as an independent data point, but should
not be the sole decision maker for events that could move the portfolio.
**Status:** Implemented in `ai/escalation.py` (`should_escalate_to_local_llm`),
`ai/providers/rules_provider.py` (configurable `materiality_map`), `ai/router.py`
(new config params passed through), `config/ai.yaml` (materiality_map, confidence bars, gpu settings).

---

## D015 — Global opportunity replacement allocator
**Date:** 2026-04-11
**Decision:** Capital allocation is driven by a global opportunity ranking and replacement model, not by static capital sleeves, fixed position-count limits, or hard-coded exposure caps as primary logic.

The system must continuously compare (1) current positions using capital and (2) new candidate opportunities from all enabled strategies and brokers. If a candidate offers materially better expected value than one or more held positions, the system may reduce or close those positions—including small winners, flat P&L, or controlled small losses—to fund the stronger opportunity.

**What changes:**
- No fixed maximum position count as a primary trading rule
- No primary rejection path of “no free capital” when better opportunities exist
- No hard strategy sleeve barriers as primary allocation logic (sleeves remain optional for attribution/reporting)
- Held positions are always eligible for reduction or replacement; capital is continuously contestable

**What remains true:**
- The risk engine retains unconditional veto for ruin prevention, operational integrity, invalid market state, broker rejection, margin danger, impossible execution, or system-health failure
- AI advises, scores, and explains only; it never executes directly
- All replacement decisions, rejections, and reallocations must be logged with reasoning
- `Decimal` for prices, quantities, fees, P&L, and target weights

**Allocation philosophy:**
- Gross exposure, concentration, leverage, and replacement aggressiveness are computed outputs from regime, opportunity scores, liquidity, volume anomaly, breadth, drawdown, and execution quality
- Defender / Trader / Hunter shape behaviour through policy coefficients (see `config/profile_modes.yaml`), not static mode caps; explicit safety bounds remain configurable emergency rails only

**Operational question:** Not “Do we have spare cash?” but “Is this opportunity better than the weakest current use of capital?”

**Reason:** Static sleeves and fixed exposure buckets block the proactive, speculative reallocation the system is intended to support. This decision aligns implementation with layered parameters and auditable risk while preserving risk-engine supremacy.

**Status:** Implemented end-to-end: primary trading path in `system/trading_loop.py` batches `SignalCandidate`s → regime → `build_opportunities_async` → `build_allocation_decision` (replacement interval + churn from `config/allocation.yaml`) → `apply_allocation_smoothing` → `build_execution_plan` → `risk_signal_from_execution_instruction` → existing `RiskEngine` + `ExecutionEngine`. Volume escalation enqueues `d015_volume_refresh` on `CommandBus`; the next cycle merges refreshed features via `drain_volume_refresh_features`. `ALLOCATOR_D015_LEGACY_FALLBACK=true` restores the per-symbol legacy signal path. When `allocator_d015_primary` is active (default), `risk/engine.py` skips allocator-duplicative checks; kill switch, min order, drawdown/daily loss, proportionality, confidence, asset class limits, M8 guards remain.

**Env:** `ALLOCATOR_D015_SHADOW=true` logs legacy vs D015 summary (legacy path only). `ALLOCATOR_D015_LEGACY_FALLBACK=true` forces legacy loop. See `docs/D015_VALIDATION.md` and `scripts/d015_paper_report.py`.

---

## D012 — Funding rate arbitrage as first arbitrage strategy
**Date:** 2026-04-12
**Decision:** First arbitrage module is funding-rate carry (long spot / short perpetual) with broker-agnostic venue discovery via `data/capability_registry.py` and `execution/venue_selector.py`. Optional perp snapshot support lives on concrete adapters (e.g. Bybit linear `fetch_funding_market_snapshot`) without extending `brokers/base.py`.
**Reason:** Lower latency sensitivity than cross-exchange spot scalping; fits multi-broker adapters and pre-funded treasury model; structural edge is funding, not price prediction.
**Status:** Implemented (scan + signal + risk hooks + execution routing). Enable under `config/strategies.yaml` / `risk_limits.yaml` when ready.

---

## D013 — Strategy coordination layer above strategy outputs
**Date:** 2026-04-12
**Decision:** Add `portfolio/opportunity_book.py`, `portfolio/strategy_coordinator.py`, and `portfolio/capital_scheduler.py` to rank `StrategyOpportunity` objects across strategies before capital allocation. Coordinator ranks only; risk engine and execution paths remain authoritative.
**Reason:** Reduces strategy conflict, preserves optional reserve for short-lived arb, allows regime-weighted priority without bypassing risk veto.
**Status:** Implemented (library components; optional wiring into `system/trading_loop.py` later).

---

## D014 — Global edge coordinator vs D015-as-final allocator
**Date:** 2026-04-12
**Decision:** When `GLOBAL_EDGE_COORDINATOR=true` (or `enabled` in `config/global_edge.yaml`), the trading loop may use `portfolio/global_edge_coordinator.py` to rank held positions (`HeldPositionEdge`) and new `StrategyOpportunity` rows (directional batch + optional funding/cross-exchange arb scans) and emit **incremental** `CoordinatorAction`s only. Deployment intent for that tick comes from this coordinator; `build_allocation_decision` is skipped for that tick when the global-edge path runs. Coordinator output is converted via `signals/arb_bridge.py` into unified signals, then **ExecutionPlanner** (cross-exchange), **RiskEngine**, and **ExecutionEngine** unchanged — no risk bypass, no change to `brokers/base.py`.
**Reason:** Single place to compare “remaining edge” in existing positions vs new opportunities (including arb) under mode thresholds, without full liquidation/re-allocation in one step; keeps D015 available when the flag is off.
**Status:** Implemented behind env/YAML; `ENABLE_ARBITRAGE` gates arb scans; treasury snapshot merged via `portfolio/treasury_manager.merge_treasury_into_portfolio_state`.

---

## D016 — IBKR single-leg options (structured instrument, no strategy engine yet)
**Date:** 2026-04-12
**Decision:** Support US equity-style single-leg options on IBKR with a first-class `OptionContractSpec` (`core/instruments.py`), optional `Order.instrument_metadata` / `Position.instrument_metadata` on the frozen adapter models for serialization only, IBKR-specific chain/qualify/market-data helpers on `IBKRAdapter`, and a dedicated `options_trading` risk gate (`risk/engine.py` + `config/risk_limits.yaml` + env overrides in `risk/options_env.py`). No multi-leg, no Greeks/IV engine, no AI option reasoning; default policy is long-only opens in paper-first mode with explicit rejection reasons.
**Reason:** Options permissions are available on the account; the system must represent contracts cleanly, route orders through the same risk → execution path, persist option metadata for audit, and stay conservative until dedicated strategy and surface work exists.
**Status:** Implemented. Enable with `ENABLE_OPTIONS=true` and tighten limits via `OPTIONS_*` env vars or YAML.

---

## D017 — Stateful signal accumulation (per-asset conviction memory)
**Date:** 2026-04-12
**Decision:** Trading signals are not purely point-in-time. The system maintains a **persistent, time-decayed signal state per symbol** in `signals/accumulator.py`, combining quantitative strategy inputs, rolled-up AI news scores, and macro regime bias before the unified `Signal` is produced. Decay uses half-lives per horizon; reinforcing horizons increase conviction; divergence reduces confidence. The risk engine remains the final authority; the accumulator does not bypass risk.
**Reason:** Markets reflect accumulated information; several weak aligned inputs are often more meaningful than a single headline. Explicit state makes behaviour auditable and explainable.
**Implication:** `SignalEngine` accepts an optional `SignalAccumulator`; `config/strategies.yaml` `signal_engine.use_signal_accumulator` enables the path; `system/trading_loop/` / `run_m3.py` / `run_m5.py` ingest `AIPipelineResult` into the accumulator each AI cycle.
**Note:** `docs/DECISIONS.md` currently contains duplicate **D012–D014** section numbers after D015 (arb / coordinator entries). Renumber those in a dedicated doc cleanup; do not reuse those IDs for new decisions.

---

## D018 — Trading loop package, fast control commands, broker degradation
**Date:** 2026-04-12
**Decision:** (1) The orchestrator trading loop lives in the `system/trading_loop/` package (`TradingLoop` in `loop.py`, shared YAML/volume helpers in `helpers.py`) instead of a single oversized `trading_loop.py` module. (2) Control commands (`kill`, `set_parameter`, etc.) are processed on a short interval (`CONTROL_COMMAND_POLL_SEC`, default 2s) via a dedicated asyncio task so long iterations do not delay kill/parameter updates. (3) Execution auto-fail and reconciliation auto-fail default to **per-broker disable** (`RiskEngine.disable_broker`) rather than global kill; `EXECUTION_AUTO_KILL_GLOBAL=true` restores the old global kill behavior. (4) Optional `brokers` lists on kill/reset API payloads disable or re-enable specific venues without a full global kill.
**Reason:** Maintainability, operational responsiveness, and isolation when one venue fails while others remain healthy.
**Status:** Implemented.

**Risk parameter persistence (unchanged contract):** Regime overrides from the dashboard/API still merge into `ControlState`, persist to `config/risk_parameter_overrides.yaml` on successful `set_parameter`, and reload from disk on `ParameterManager` init; `hydrate_risk_parameters_from_bus` restores in-process state at runner startup.

---

## D019 — Dashboard “control tower” snapshot + period P&L
**Date:** 2026-04-12
**Decision:** The React dashboard (`ui/`) prioritises **decision transparency** over decorative charts. The trading loop persists a JSON snapshot to Postgres `ControlState` under key `dashboard.snapshot` (`system/dashboard_publish.py`): D015 path publishes opportunities, `RegimeState` components, `AllocationDecision` (including `allocation_targets`), `ExecutionPlan`, and `PortfolioState` pressure fields; the global-edge path publishes ranked `StrategyOpportunity` rows, held edges, and coordinator actions. `SignalAccumulator.dashboard_snapshot()` adds ranked conviction for the same payload. The API exposes `GET /dashboard/snapshot`; `GET /pnl` adds **calendar** week-to-date and month-to-date sums from `daily_pnl` (same `date` convention as `today`) plus lightweight `metrics` (win-rate on days with trades, max drawdown on stored portfolio value series when enough points exist). WebSocket `tick` frames include a small `dashboard` hint for change detection.
**Reason:** The operator must see allocator intent, risk outcomes, and capital context on one screen without reading logs; period P&L answers “how am I doing this week/month” without ad-hoc spreadsheets.
**Status:** Implemented.

---

## D020 — Unconditional dashboard heartbeat when allocator snapshot is skipped
**Date:** 2026-04-12
**Decision:** When a loop iteration does **not** run the full D015 or global-edge dashboard publish (e.g. `batch_candidates` empty, legacy per-symbol path, or publish failure), the loop still writes `dashboard.snapshot` via `publish_dashboard_snapshot_heartbeat()` in `system/dashboard_publish.py`. Payload includes `heartbeat_only`, `dashboard_feed` (`reason`, `message`, symbol/feature counts, batch size), empty `opportunities` / `allocation`, and current `portfolio` serialization so `GET /dashboard/snapshot` reflects each tick. The UI shows a short banner in `SignalBrain` when `heartbeat_only` is true.
**Reason:** Conditional publish made an empty feature store or mis-scoped universe **indistinguishable** from a genuinely quiet market; the API could return `{}` or a stale snapshot with no diagnostic.
**Status:** Implemented (`system/trading_loop/loop.py`; `_run_global_edge_tick` returns `(executed, dashboard_snapshot_written)` so heartbeat runs if global-edge publish fails). **`GET /system/status`** merges `trading.snapshot_published_at` from the same `dashboard.snapshot` `updated_at` so the UI can treat staleness against the loop clock without relying only on the last HTTP snapshot fetch.

---

## D021 — Shared DB pool, execution engine registry, signal veto Decimal hygiene
**Date:** 2026-04-18
**Decision:** (1) FastAPI startup calls `storage.db.bind_app_database(engine, session_factory)`; the trading loop prefers `get_app_database()` and **does not** open a second async engine when the API already bound one (still creates its own `CommandBus` wrapper over the shared factory). On loop-only entry points without a prior bind, behaviour is unchanged (`init_async_database`). The loop **only** disposes an engine it created (`owns_engine`); shared engines are never disposed from the loop. (2) `ExecutionEngine.__init__` registers `set_execution_engine(self)`; the loop clears it in `_run` `finally`. (3) `SignalEngine` news veto / confidence blending uses `Decimal` for thresholds and overlay scores; accumulator metadata stores string decimals; `accumulator_dual_ai_veto` no longer stacks a point-in-time news veto when an accumulator `NetSignal` exists. (4) IBKR option build uses `float(str(spec.strike))` from `Decimal` strike. (5) Orchestrator persists `last_start_error` across `errors.clear()` on retry start; status exposes it. (6) UI: first-cycle wait copy on `LiveStrip`, `last_start_error` on `error`, clear `snapshotFetchFailed` when not running.
**Reason:** Audit P0-1/P0-2/P0-5/P0-6/P0-7/P1-9/P1-10; reduce silent double pools, fix `/status` execution visibility, and avoid float drift in veto math.
**Status:** Implemented.

---

## D022 — Late venues on execution engine + AI health on status
**Date:** 2026-04-18
**Decision:** (1) `ExecutionEngine` accepts optional `broker_manager`; `_get_broker` prefers an already-connected adapter from `broker_manager.adapters` before calling `get_broker`, so late connects (e.g. IBKR) reuse the same instance as routing/reconciliation instead of a duplicate client. `TradingLoop._check_late_brokers` calls `execution_engine.add_allowed_broker(name)` so reconciliation preload includes the venue. (2) `AIRouter` / `NewsClassifier` expose `runtime_ai_status()`; `publish_runner_heartbeat` adds an `ai` object (kind, `providers_enabled`, `ai_degraded`); `GET /system/status` merges that `ai` blob into `trading` when `runtime.heartbeat` is present.
**Reason:** Audit P1-1 (late broker vs execution) and P1-8 (observable AI degradation without log diving).
**Status:** Implemented.

---

## D023 — Pytest read bypass, Docker retries, pipeline cancellable sleep, STARTING hint, IBKR jitter
**Date:** 2026-04-18
**Decision:** (1) Dashboard read middleware skips checks when `PYTEST_API_DISABLE_READ_MIDDLEWARE` is truthy; `tests/conftest.py` sets default `1` so TestClient works with a developer `.env` that defines `DASHBOARD_READ_TOKEN`. Tests that assert read protection call `monkeypatch.delenv("PYTEST_API_DISABLE_READ_MIDDLEWARE", raising=False)` first. (2) `dependency_manager._start_docker_service_with_retries` wraps `docker compose up -d` for `db` / `redis` (attempts `DOCKER_COMPOSE_UP_ATTEMPTS`, default 3; backoff + jitter between attempts). (3) Orchestrator pipeline uses `Orchestrator._sleep_cancellable` (~2s chunks) instead of one long `asyncio.sleep` so stop/cancel responds within seconds. (4) While `state == starting`, `Orchestrator.status()` adds `trading.orchestrator_starting: true`. (5) On `place_order` retries for broker `ibkr`, `ExecutionEngine` adds uniform jitter up to `IBKR_PLACE_ORDER_RETRY_JITTER_SEC` (default 0.5s) after linear backoff.
**Reason:** Audit P1-4/P1-5/P1-6/P1-7 and pytest stability; reduce TWS burst sensitivity on reconnect retries.
**Status:** Implemented.

---

## D024 — Live equity curve uses broker `get_last_price`, intraday client buffer
**Date:** 2026-04-21
**Decision:** `/pnl`'s `today.portfolio_value` is now re-computed on every poll by marking the latest `PositionLog` snapshot to the freshest available price. Price priority in `api.server._compute_live_unrealised_mtm`: (1) broker live `get_last_price(symbol)` — raced across **all** connected adapters via `_live_broker_prices`, first non-zero result wins with a 1.5s per-adapter timeout; (2) `FeatureSnapshot` latest close (hourly bar fallback); (3) `PositionLog.current_price`; (4) average entry (no movement). The redesign dashboard hook (`ui/src/app/redesign/useLiveSystem.ts`) samples the live NAV into a rolling intraday buffer (`liveNavSamples`, capped at `MAX_LIVE_NAV_SAMPLES = 360` ≈ 1h at 10s cadence) and blends it with `/pnl/history` daily rows into the hero `EquityCurve`. `EquityCurve` was also repadded (`padX=10`, `padY=6`, rounded stroke, non-scaling stroke width) so the pulsing endpoint dot never clips against the card's right edge.
**Reason:** Backend previously drove NAV from hourly `FeatureSnapshot` bars plus cached broker equities; a stale position on an idle venue froze the hero curve flat for hours. Racing adapters avoids being pinned to a 15-minute-delayed IBKR paper feed when Alpaca's IEX feed is live, and the client buffer guarantees ≥ 2 points as soon as the system runs so the curve always renders as a time-series (even when the price is flat between ticks).
**Status:** Implemented. Covered by `tests/test_live_broker_prices.py` (8 cases — fallback order, zero/exception handling, timeout isolation, multi-symbol resolution).

---

## D025 — Full strategy roster on `/system/status` (idle strategies remain visible)
**Date:** 2026-04-21
**Decision:** `TradingLoop.status_dict()` (→ `Orchestrator.status()` → `GET /system/status`) publishes `loaded_strategies: [{name, enabled, kind}]` covering every strategy the loop has registered — signal strategies (`momentum_breakout`, `mean_reversion`) **and** the arbitrage stack (`funding_rate_arbitrage`, `cross_exchange_arbitrage`). The redesign hook (`useLiveSystem.ts`) fetches intelligence signals at the endpoint's max (`limit=50` vs previous `16`) and merges the roster into the UI's strategy list via `mergeStrategiesWithSignals(snapshotStrategies, sigs, loadedStrategies)` in `mapping.ts`. Strategies with no opportunities in the current snapshot / signal window are rendered as zero-weight `idle` cards on `StrategiesScreen`, with an "arbitrage" pill for the arb stack and a "disabled" pill when `strategy.enabled=false`.
**Reason:** Previously the Strategy Mix card showed only strategies that had produced opportunities in the current allocator snapshot (or appeared in the newest 16 signals). During a regime where one strategy dominates (e.g. mean-reversion in a low-vol day), other registered strategies silently disappeared from the UI and the operator had no way to distinguish "strategy not running" from "strategy running but idle". Exposing the loop's registry makes the full system behaviour observable without reading logs.
**Status:** Implemented. Covered by `tests/test_loop_status_strategies.py` (6 cases — signal-only, arbitrage-only, missing `enabled` attr fallback, default arb display names, explicit `name` attr override, combined roster).

---

## D026 — In-flight order dedup + accurate position size / exposure display
**Date:** 2026-04-21
**Decision:** (1) `ExecutionEngine.execute()` consults the `orders` table before placement and short-circuits when a non-terminal order for the same `(symbol, side, broker)` exists within `EXECUTION_DEDUP_WINDOW_SEC` (default 900s). Statuses considered in-flight: `pending`, `open`, `partially_filled`. Skipped attempts increment `engine.dedup_skipped` and emit a single `DEDUP SKIP` log line with the existing order id and age. DB lookup failures fall through (best-effort — trading must not hang on a DB hiccup). (2) The dashboard `mapPositions()` prefers the authoritative `quantity` field returned by `/positions` and populates `Position.notional = |qty × last|`; the legacy `unrealised_pnl / (last − avg)` heuristic is kept only as a last-resort fallback. `Position.broker` is now surfaced so the Book row can attribute each holding to its venue. (3) `mapExposure()` and `numFromPortfolio()` auto-detect the unit of `portfolio.gross_exposure` / `net_exposure` (ratio, percent, or absolute £) and normalize by `nav` when the value exceeds 100 — previously absolute £ figures were silently clipped to `100%`, making the Exposure and Capital-at-work panels unusable. (4) `BookScreen` computes `deployedCapital` as `Σ positions.notional` instead of `nav × exposure.gross`, which was unreliable when exposure was mis-parsed; the row header now explicitly labels Symbol / Size / Avg / Last / P&L / Weight / Trend.
**Reason:** Auditing a paper run that showed one COHR fill plus 29 `pending` IBKR orders revealed three stacked issues: the allocator was re-emitting identical orders each loop (FUTY×8, FLMI×5, BFAM×4 duplicates); the Book row showed only `qty` with no notional, forcing the operator to compute size mentally; and the backend ships `gross_exposure` as absolute £ (e.g. `57919.88` against `nav=1055095.72`) which `parsePct()` mis-read as a percent and clipped to 100%, inflating Capital-at-work to nonsensical figures. Fixing all three at once restores operator trust in the Book view and eliminates the duplicate-order flood at the source.
**Status:** Implemented. Covered by `tests/test_execution_dedup.py` (5 cases — skip on in-flight, allow when clear, disable via env, DB-failure fall-through, no session-factory). UI `mapping.ts` helpers are plain functions unit-testable from Node; exposure normalization matches the same auto-detect rules used in `numFromPortfolio`.

---

## D027 — Wake the hunter: marketable limits, multi-asset-class routing, mode-aware cadence, forex coverage, admin cancel
**Date:** 2026-04-21
**Decision:** Five coupled changes that remove the structural reasons the system was placing one-a-day limit orders against a single asset class on a single broker:

1. **Marketable-limit rewrite at placement time.** `ExecutionEngine._apply_marketable_limit()` runs *after* `_build_order` and *before* `_normalize_order_for_broker`. It fetches live top-of-book via `broker.get_order_book(symbol, depth=1)` and rewrites the order's limit price to `ask × (1 + slip)` on BUY / `bid × (1 − slip)` on SELL. Falls back to `get_last_price` when the book is empty, then to the original `suggested_price` as last resort. Slippage buffer tunable via `EXECUTION_MARKETABLE_SLIP_BPS` (default `10` bps, `0` disables). Observability via `engine.marketable_adjusted` counter and a single `MARKETABLE LIMIT` info log per adjustment.

2. **Multi-asset-class strategies.** `Strategy.__init__` now consumes either the legacy scalar `asset_class` or the new list form `asset_classes: [equity, crypto, forex, future]` from YAML, exposing `supports_asset_class(ac)` that the loop uses as a gate before calling `generate_signal(symbol, df)`. Both `momentum_breakout` and `mean_reversion` declare `[equity, crypto, forex, future]`. The loop relabels every signal with `asset_class_for_symbol(symbol)` so `SmartOrderRouter` picks the venue per class (equity→ibkr/alpaca, crypto→binance/kraken, forex→ibkr).

3. **Mode-aware loop cadence.** `config/profile_modes.yaml` gains `loop_cadence_sec: {defender: 900, trader: 300, hunter: 120}`. `TradingLoop._load_mode_cadence_map()` validates + floors to 10s, and each iteration picks the current mode's cadence before `await asyncio.wait_for(stop_event, timeout=…)`. Mode switches take effect on the very next sleep — no restart needed. `ProfileModesConfig` in `config/models.py` was extended to accept the new key (pydantic `StrictBaseModel`).

4. **Forex + futures coverage.** `config/data_pipeline.yaml` seeds `EURUSD=X`, `GBPUSD=X`, `USDJPY=X`, `AUDUSD=X`, `USDCHF=X`, `USDCAD=X` for forex and `ES=F`, `NQ=F`, `YM=F`, `CL=F`, `GC=F`, `SI=F` for futures. New helpers `is_forex_symbol`, `is_futures_symbol`, `asset_class_for_symbol` classify them. `broker_symbol_for(symbol, broker)` strips the yfinance `=X` / `=F` suffix before the order reaches the broker, so `EURUSD=X` enters the IBKR adapter as `EURUSD` and maps cleanly to `Forex("EURUSD")`. Futures execution is **gated** behind `FUTURES_EXECUTION_ENABLED=0` (default off) until a contract-month resolver ships — data flows and signals are still logged/ranked for observability.

5. **Admin cancel-pending-orders.** `POST /admin/cancel_pending_orders` (optional `?broker=` filter, guarded by `X-Control-Token`). For every connected broker it calls `get_open_orders()` + `cancel_order()` per id, then bulk-updates the `orders` table: `WHERE status IN ('pending','open','partially_filled') → status='cancelled'`. Returns `{cancelled_by_broker, db_updated}`. Intended for one-shot use after execution-semantic changes (like this one) to clear the pre-change backlog and let the new dedup + marketable-limit logic start clean.

**Reason:** The diagnostic walk-through on 2026-04-21 showed a paper session with 218 orders in 12h, of which 1 (COHR) filled, 94 sat `pending` on IBKR at unmarketable bid prices, and every single signal was equity-on-IBKR even though binance/kraken/alpaca/bybit were connected. Root causes, in descending impact: (a) limit price was the 1h-bar close, never the current ask; (b) both signal strategies were hard-pinned to `asset_class: equity, preferred_broker: ibkr`; (c) `loop_interval_sec` was fixed at `120s` regardless of profile mode so "hunter" rotated the same as "defender"; (d) the universe excluded forex/futures entirely; (e) the pre-change stuck-order backlog kept re-blocking deduplication even after fixes. Shipping all five together is the only way to actually observe the hunter behaving like a hunter instead of a sleepy sniper at one equity price from an hour ago.

**Status:** Implemented. Covered by:
- `tests/test_execution_marketable_limits.py` (8 cases — buy bumps above ask, sell drops below bid, slip=0 disables, MARKET untouched, last-price fallback, no-reference preserves order, book exception tolerated, no-broker passthrough).
- `tests/test_asset_class_routing.py` (16 cases — symbol → asset-class classification for all 4 classes, strict forex/futures guards, `broker_symbol_for` passthrough + suffix stripping, multi-class strategy declaration, legacy single-class back-compat, empty-config default).
- `tests/test_mode_cadence.py` (7 cases — YAML load, missing block, invalid-entry filter, 10s minimum floor, `_read_active_mode` default, `_read_active_mode` JSON read, defender≥trader≥hunter invariant).
Full suite still green (276 passed, 3 skipped).

**Operator follow-ups (next cycle):**
- IBKR futures contract resolver (`ES=F` → `Future("ES", "<current-month>", "CME")`) to flip `FUTURES_EXECUTION_ENABLED=1`.
- Surface `marketable_adjusted` / `dedup_skipped` on `/system/status` so the UI can show "N orders priced-to-market / M deduped this hour".
- Treat stale pending orders > N minutes as auto-cancellable without operator interaction.

---

## D028 — Honest broker coverage: partial-NAV transparency + auto-disable on exclusion
**Date:** 2026-04-22
**Decision:** The aggregated NAV on the dashboard is now **coverage-aware**, and the risk engine auto-disables any broker that is not contributing to it. Three coordinated changes:

1. **Backend coverage contract.** `BrokerReport.coverage()` returns `{full, configured, included, excluded: [{name, connected, balance_ready, reason}]}`. `full` is true iff every configured broker is both connected and balance-ready — i.e. NAV truly reflects all wallets the operator asked for. `included` lists the brokers whose balances are in NAV right now; `excluded` carries the failing brokers with the concrete error from `BrokerStatus.error` (e.g. `"Startup connect deferred (transient exchange throttle/retry)"`). The orchestrator's `status()` exposes this as a top-level `coverage` key on `GET /system/status` (and therefore the WebSocket `tick.system` payload).

2. **Risk engine auto-sync.** A new orchestrator background task `_coverage_sync_loop` (tick `COVERAGE_SYNC_INTERVAL_SEC`, default 5s) diff-applies coverage transitions onto `RiskEngine._disabled_brokers`: every excluded broker gets `risk.disable_broker(name)`, every freshly-included broker gets `risk.enable_broker(name)`. This guarantees no new orders are routed to a broker whose position state is stale — the same gate used by the kill switch, so the existing `_check_broker_disabled` risk rule covers it with zero additional checks. Idempotent, cancellable, survives a missing risk engine (pre-loop phase) with a no-op cycle. Stopped cleanly in `Orchestrator.stop()`.

3. **UI "honest degrade".** `BrokerStatus.state` expanded from `live | warming | off` to `live | warming | offline | off`. `mapBrokers` now distinguishes a broker that is genuinely still connecting (no error, pill = `warming` / caution tone) from one that is down with a concrete failure (error present, pill = `offline` / danger tone with the backend's error surfaced on hover via `title`). The NAV card on the Dashboard renders an amber **"Partial NAV"** banner whenever `coverage.full === false`, naming the excluded brokers and exposing each one's reason on hover, plus a compact footnote `· partial NAV (excl. kraken)` next to the Tradable / Allocation chips. `useLiveSystem` exports a `coverage: Coverage` field so other screens (Book, Risk, Log) can reason about which venues are in the NAV.

**Reason:** On 2026-04-22 the system transitioned to `RUNNING` with IBKR showing a `warming` pill while its Gateway was actually not running at all — NAV read £98k instead of the real £1.05M because the aggregator had silently skipped the IBKR wallet, and the UI gave no indication that anything was wrong. The "warming" state conflated three meaningfully different backend conditions — genuinely connecting, connected but no balance yet, and configured-but-offline-with-an-error — and the orchestrator flipped to `RUNNING` the moment *any* broker was live, which is correct for trading availability but misleading for NAV interpretation. The right system behaviour is (a) always show an aggregated NAV for whatever wallets are currently trustworthy, (b) tell the operator explicitly that NAV is partial and which wallets are missing with their concrete reasons, and (c) refuse to route new orders to excluded brokers at the risk layer so partial coverage cannot drift into partial exposure.

**Rejected alternatives:** "Block `RUNNING` until every broker is up" (Option A): hides the working capital stack behind one broker's outage, which is exactly the opposite of the multi-venue architecture's value. "Kill-switch on partial coverage" (Option B): indistinguishable from a real emergency and would stop strategies on correctly-attributed wallets. Both failed the review because the real failure mode is *unknown* partial coverage, not partial coverage per se.

**Status:** Implemented. Covered by:
- `tests/test_broker_coverage.py` (10 cases — full coverage, partial-one-down, connected-but-no-balance-ready, unconfigured broker ignored, empty-configured is not "full", status dict shape with/without report, risk-engine disable on exclude, re-enable on recovery, no-risk-engine graceful no-op).
- Backend suite remains green (323 passed, 3 skipped).
- UI builds clean under Vite + TS strict.

**Operator follow-ups (next cycle):**
- Extend the coverage contract to the Risk screen: render an "excluded from NAV" chip on the Capital-at-work row when `coverage.full === false`.
- `POST /admin/retry_broker/{name}` to force an immediate reconnect attempt on an excluded broker instead of waiting for the background reconnect loop — operator can one-click recover after launching IB Gateway.

---

## D029 — Single canonical NAV aggregator (BASE-aware) + periodic heartbeat persistence
**Date:** 2026-04-22
**Decision:** The aggregated NAV is now computed in **exactly one place** and persisted on a cadence so shutdown can never lose it. Two coordinated changes:

1. **Single source of truth for live NAV.** `api/server.py::_live_portfolio_value()` used to duplicate (and subtly mis-implement) the trading-loop's broker aggregation: it took `max(balances)` per adapter, which for IBKR picked a single cash-currency line instead of the `BASE` row that carries `NetLiquidation`. The result was that `/pnl.today.portfolio_value` (which the UI NAV card reads) reported ~£884k while `/dashboard/snapshot.portfolio.nav` (built off the trading-loop aggregation) correctly reported ~£1,055k — a £170k phantom loss. `_live_portfolio_value()` now delegates to `system.portfolio_equity.live_portfolio_value()` — the canonical BASE-preferring helper that already backed the trading loop. One function, one behaviour, two callers.

2. **NAV heartbeat (periodic + on-shutdown).** Before this change, `daily_pnl` only received a row when a trade filled. A quiet trading day plus an ungraceful shutdown (OS kill, power loss, crash) could leave the DB with either no row for today or a stale one from yesterday, so the `/pnl` DB fallback had nothing fresh to show when brokers were slow to report balances post-restart. A new orchestrator background task `_nav_heartbeat_loop` (tick `NAV_HEARTBEAT_INTERVAL_SEC`, default 60s) calls `_upsert_daily_pnl` with the live BASE-aware equity every minute, and `Orchestrator.stop()` flushes one final heartbeat with a 10s timeout before disconnecting brokers. A tick that sees zero aggregated equity is a **no-op** — we never clobber a valid historical row with a spurious zero. `/pnl` also now falls back to the most recent persisted row (any date) if today's is missing, so the UI still shows a meaningful NAV during the all-brokers-still-connecting window after a restart.

**Reason:** On 2026-04-22 the operator reported NAV had "dropped by nearly £200,000 overnight" (£880k vs yesterday's £1M+). Root cause was entirely cosmetic — no capital had been lost; the UI was reading a buggy duplicate aggregator that understated IBKR's balance by ignoring the `BASE` NetLiquidation row. Secondary concern: "is it not secure to turn off the system?". It was safe (`daily_pnl` preserves yesterday's row on any shutdown) but the persistence cadence was fragile — tied to trade fills only — which created the exact class of "last persisted value is stale" failure modes that make honest NAV reporting impossible. Consolidating the aggregator and adding a heartbeat closes both gaps and makes the answer to "what is my NAV?" the same number from every code path.

**Rejected alternatives:** "Keep the two aggregators and add a test that they agree" — still ships two ways to make the same mistake. "Emit NAV only when exposure changes" — does not solve the empty-trading-day case that triggered the report.

**Status:** Implemented. Covered by:
- `tests/test_live_portfolio_value.py` (10 cases — zero/empty inputs, BASE preference over larger cash rows, BASE even when smaller than non-BASE, `max` fallback when no BASE row, per-adapter dedup, zero rows skipped, adapter exceptions swallowed, case-insensitive BASE match, API ↔ loop consistency).
- `tests/test_nav_heartbeat.py` (5 cases — upsert on non-zero equity, skip on zero equity, swallow DB errors, loop cancellable, idempotent start).
- Full backend suite: 344 passed, 3 skipped.
- Live reconciliation post-fix: `/pnl.today.portfolio_value` = `/dashboard/snapshot.portfolio.nav + today_unrealised` to the penny.

**Operator follow-ups (next cycle):**
- Consider surfacing `daily_pnl` as a TimescaleDB hypertable to make multi-year NAV history queries cheap.
- Surface "last NAV heartbeat" timestamp on the System screen so operators can spot a stuck heartbeat before it causes drift.

---

## D031 — NAV allowlist + live wins over stale `daily_pnl` (coverage / kill alignment)
**Date:** 2026-04-24
**Decision:** Two fixes that were stacking and making headline NAV "sticky" or inconsistent with broker coverage.

1. **`system.portfolio_equity.live_portfolio_value` must respect the same inclusion rules as the rest of the system.** It used to sum every object in `broker_manager.adapters` with no filter, so a venue could still add `NetLiquidation` after it was excluded from coverage (e.g. risk `disabled_brokers` after `coverage_sync_loop` or `POST /kill` with `brokers: ["ibkr"]`). The helper now:
   - includes only names in `BrokerManager.report.included_names` (connected + balance_ready), and
   - skips any name in `RiskEngine.disabled_brokers` (lowercased match).
   Stubs that omit `report` keep the previous "all adapters" behaviour for unit tests.

2. **`GET /pnl` headline `today.portfolio_value` no longer does `max(live, db, last_persisted, …)` when `live > 0`.** The `max` was added (D029) to avoid a drop to the `PORTFOLIO_VALUE` default while brokers reconnected, but it also pinned the UI to an old `daily_pnl` row that was written when an excluded broker still inflated the (pre-allowlist) live sum. If `live_value > 0` from the broker sum, that value is the display headline; `daily_pnl` is only used when `live_value` is still zero (first snapshot / cold path).

**Reason:** Operators saw ~£1.05M "forever" or numbers that ignored kill/coverage, because the UI floor and the adapter dict could both ignore exclusion.

**Status:** Implemented. `tests/test_live_portfolio_value.py` extended (allowlist + `disabled_brokers`); full suite green.

**Follow-up (IBKR single-currency rows):** If IB returns only `currency=USD` rows (no `BASE` line), `system.portfolio_equity` cannot disambiguate cash vs NAV. `brokers/ibkr/adapter.py` must pick **NetLiquidation** before **TotalCashValue** / **CashBalance** when building each `Balance.total` from account-summary tags; otherwise a USD cash line (~884K) wins over NetLiq (~1,055K). See `brokers/ibkr/adapter._total_from_account_summary_tags` and `tests/test_ibkr_summary_tags.py`.

**Follow-up (`run_m3._load_portfolio_state`):** The same `max(live, db)` anti-pattern lived in `run_m3._load_portfolio_state` (used by the NAV heartbeat and trading loop for `portfolio_state` / dashboard `nav`). It re-pinned stale `daily_pnl` into every snapshot and upsert, self-refreshing 884K. `run_m3._resolve_portfolio_value_for_state` now mirrors **GET /pnl**: when `fallback_portfolio_value` (live, post-allowlist) is **> 0**, it wins; DB is used only when live is still 0. See `tests/test_run_m3.py` (`test_resolve_portfolio_value_*`).

---

## D032 — `regime_strategy_gates` must list every live signal strategy
**Date:** 2026-04-24
**Decision:** `config/ai.yaml` `pipeline.regime_strategy_gates` lists, per `macro_regime` label, which `RawSignal.strategy` values survive `ai.regime.filter_by_allowed_strategies` in `system/trading_loop/loop.py` **before** `_pick_best_signal`. The previous lists only included `momentum_breakout` and/or `mean_reversion`, so **volume_flow**, **event_driven_news**, **pairs_trading**, **volatility_regime**, and **regime_rotation** were **dropped every tick** whenever the AI returned a known regime key — they looked “off” in the Strategy mix even though the loop and `config/strategies.yaml` had them enabled.

**Change:** All five regime keys use the same YAML anchor `default_signal_strategies` (seven names aligned with `strategies/*Strategy.name`). Operators who want a truly defensive sub-roster in `risk_off_stagflation` / `tightening` should **edit** that list rather than shipping an incomplete one by accident.

**Status:** Implemented. `tests/test_ai_pipeline.test_ai_yaml_regime_gates_lists_core_signal_strategies` guards drift.

---

## D033 — Multi-strategy candidates, `strategy_candidate_log`, coordinator per-symbol dedupe

**Date:** 2026-04-24
**Decision:** The batch (D015 / global-edge) path no longer calls `_pick_best_signal` before building the candidate set. For each symbol, every enabled strategy that returns a raw (or a logged skip) is visible: rows go to table `strategy_candidate_log` via `system/strategy_candidate_log.py`, separate from execution-path `SignalLog` (no change to meta_adaptation joins). The global-edge coordinator receives all `StrategyOpportunity` rows, then `dedupe_opportunities_by_symbol` keeps the highest `priority_score` per symbol before `propose_actions` (arbitrage sleeve names are excluded from same-symbol collapse). Legacy per-symbol mode still executes one signal per symbol but logs `lost_to_strategy` for non-winners. `event_driven_news` logs `ai_result_unavailable` when the AI cycle did not produce a result. API: `GET /diagnostics/strategy-candidates?since_hours=24`.

**Status:** Implemented. `tests/test_strategy_candidate_flow.py` covers dedupe.

**D033b (2026-04-24):** The redesign **Strategy mix** card consumes `GET /diagnostics/strategy-candidates?since_hours=24` (see `fetch_strategy_mix_diagnostics` in `system/strategy_candidate_log.py`): per-strategy counts, `last_evaluated_at` / `last_generated_at`, `top_skip_reason`, and a `lifecycle` key mapped in the UI (Scanning / Finding setups / Competing / Selected / Trading / Blocked by risk / Idle). “Idle” in the UI means **zero evaluation rows in the window**, not “no trade.” D015 non-global (allocator primary) now logs `selected_for_allocation` for each `ExecutionPlan` instruction and reuses the shared `_process_signal` `sc_log_buffer` for `risk_rejected` and `executed` with `metadata.path=d015`. Same-symbol coordinator dedupe rows use `reason=same_symbol_dedupe` and `metadata` `{winner_score, loser_score}`.

---

## D030 — Hunter must hunt: mode-aware capital fraction + broker-truth reconciliation

**Date:** 2026-04-22
**Decision:** Two tightly-coupled fixes addressing the "sleeping hunter" symptom — the system ran with hunter regime but deployed only ~6% of tradable capital while rejecting or parking the rest.

1. **Mode-aware `max_notional_fraction_per_action`.** `GlobalEdgeCoordinator` caps each emitted open action to `opp.capital_required × frac`. Prior to D030 `frac` was a single scalar (`0.15`) applied to every mode — so hunter (which wants to deploy aggressively) got the same 15% throttle as defender (which wants risk off). A strategy asking for £44,294 was trimmed to £6,644, exactly reproducing the observed 6.4% deployment. The config value is now either a scalar (legacy, preserved verbatim) **or** a dict keyed by mode. Defaults: `hunter: 1.00` (full strategy request), `trader: 0.50` (balanced), `defender: 0.15` (defensive, matches pre-D030 uniform behaviour). `min(1, frac)` clamp guards against accidental >100% blow-ups; malformed / unknown-mode values fall back to `0.15`. Lives alongside the already-mode-aware `max_actions_per_tick` — hunter now emits up to 10 actions × full-request capital per tick, which is what the mode was designed for.

2. **Reconciliation persists broker truth unconditionally.** `ExecutionEngine._reconcile_positions_internal` compared local `PositionLog` rows against each broker's `get_positions()` output and, on any quantity divergence, logged the mismatch and returned early *before* writing the fresh remote snapshot to the DB. The unintended effect: once the DB drifted (e.g. IBKR actually held 335 COHR while our DB said 164), every subsequent reconciliation noticed the gap, logged it, and did nothing — so `GlobalEdgeCoordinator.held` permanently consumed a stale view of our holdings and kept over-proposing new opens on top of risk we already had. The fix splits the comparison from the persistence: the loop collects *all* mismatches, ALWAYS persists the remote snapshot (the broker's books are ground truth for what we own), then returns `False` after persistence so upstream still sees the divergence signal and the opt-in `auto_kill_on_reconciliation_failure` hook still fires when enabled.

**Reason:** On 2026-04-22 the operator reported hunter was "sleeping again — only 6.3% of capital working". Investigation showed 4 compounding symptoms: 41 Alpaca rejections (`insufficient buying power [code=40310000]`), 33 IBKR limits sitting pending, 94 IBKR orders cancelled with zero fills, and COHR position reconciliation reporting `local_qty=164 remote_qty=335`. The two bugs above are the direct, mechanical causes of the deployment gap:
- *Allocator bug:* even when the loop generated valid opportunities, the coordinator silently deflated their requested capital by 85%, so any single tick could only deploy ~£26k of new risk on a £1.05M NAV.
- *Reconciliation bug:* the "held" input the coordinator used to rank new vs existing edges was stuck on a stale snapshot, which over time caused the system to either double-up (proposing opens for symbols we already held larger than we knew) or under-propose (if remote quantity grew). The snapshot drift also produced the £171k COHR discrepancy visible in the logs.

**Rejected alternatives:**
- *Raise the scalar `max_notional_fraction_per_action` to 1.0.* Fixes hunter but removes the risk-off brake on defender. The per-mode dict is the same amount of config with the correct semantics.
- *Force local DB to match broker by deleting mismatched rows.* Brittle and asymmetric — it also loses the mismatch signal to upstream. The cleaner contract is "broker is truth, always persist, log and `return False` so upstream can alert / auto-kill if configured".
- *Make the coordinator re-read broker positions directly instead of DB.* Bigger blast radius, couples allocator to broker I/O, and doesn't fix the UI which also reads `PositionLog`.

**Status:** Implemented. Covered by:
- `tests/test_global_edge_coordinator.py` — 6 new cases: scalar back-compat, mode-aware per-mode fractions, `min(1, frac)` clamp at >100%, unknown-mode → trader fallback, malformed → 0.15 default, missing-key → 0.15 default.
- `tests/test_execution_engine.py` — updated mismatch test asserts persistence happened; new `test_reconcile_persists_broker_truth_on_quantity_mismatch` exercises the local≠remote case explicitly (local qty=1, broker qty=2) and asserts the persisted row carries the broker's quantity.
- Config: `config/global_edge.yaml` ships the new dict form with hunter=1.00 / trader=0.50 / defender=0.15.

**Operator follow-ups (next cycle):**
- Per-broker buying-power-aware routing: even with the allocator deflation fix, we observed Alpaca rejecting ~£440k worth of orders sized against total NAV when only IBKR had room. Sizing at the execution layer should consult each broker's `get_balances()` and either re-route or size-down rather than bounce at the venue.
- Surface position-mismatch events to the System screen so operators can see the broker-truth refresh happen in real time (currently only visible via the error log).

---

## D031 — Respect strategy sizing: end of systematic over-sizing + sizing audit trail

**Date:** 2026-04-22
**Decision:** Five tightly-scoped fixes around the sizing pipeline in the global-edge path. Together they end a multi-week bug where every directional equity signal was silently deployed at `NAV × default_position_pct` (~5% of NAV, typically £50k on £1M NAV) regardless of what the strategy actually requested — producing a systematic 7–13× over-sizing of low/medium-conviction trades and, after D030 made the coordinator deploy its full action budget, large adverse P&L swings on any signal that did not immediately move in our favour.

1. **(D031A) Respect strategy sizing in `signal_candidate_to_strategy_opportunity`.** The D015 candidate → opportunity conversion ignored `candidate.metadata["risk_notional_override"]` and `candidate.metadata["target_notional"]` and instead set `capital_required = nav * position_pct`. This silently replaced the strategy's volatility-aware, conviction-weighted sizing (e.g. £750 for a weak FCOM mean-reversion probe at ATR 0.36%; £7,913 for a COHR momentum_breakout at ATR 1.8%) with a blanket fixed-size slug. The new sizing priority is explicit:
   1. `metadata["risk_notional_override"]` if present and > 0 (most specific signal-level override)
   2. else `metadata["target_notional"]` if present and > 0
   3. else `nav * position_pct` (legacy fallback for signals that carry no sizing metadata)

   A hard ceiling of `nav × max_position_pct` (default 0.10, from `config/risk_limits.yaml`) is applied AFTER the priority pick. The ceiling is a cap only — it never inflates a smaller strategy-requested size upwards. Under-requested sizes stay small; over-requested sizes get clipped and the clip is logged.

2. **(D031B) Sizing audit trail.** Every emitted `StrategyOpportunity` / `CoordinatorAction` now carries explicit sizing-provenance fields in its `metadata`: `sizing_source` (one of `risk_notional_override` | `target_notional` | `nav_fallback`), `sizing_strategy_target_notional`, `sizing_risk_notional_override`, `sizing_proposed_base_notional`, `sizing_hard_cap_notional`, `sizing_final_capital_required`, `sizing_clipped` (bool), `sizing_clip_reason`, `sizing_nav_at_decision`, `sizing_max_position_pct`, plus post-mode-fraction fields (`sizing_pre_mode_capital`, `sizing_mode`, `sizing_mode_fraction`, `sizing_final_action_capital`). `coordinator_action_to_raw_signal` preserves them into the `RawSignal` so they survive into the order placement path. This makes every sizing decision auditable from dashboard / logs / tests — one of the reasons D031 stayed invisible for as long as it did was that nothing in the logs ever said *why* a £750 idea had become a £10,000 order.

3. **(D031C) Execution-boundary sanity guard.** A new helper `ExecutionEngine._passes_sizing_boundary_guard` runs immediately before broker placement. It rejects any order whose `abs(quantity) * limit_price` exceeds the intended `sizing_final_capital_required` by more than 1.25× (configurable by editing the helper's `tolerance` constant) or exceeds the declared `sizing_hard_cap_notional`. Arbitrage legs are exempt (capital flows via different paths). Signals without sizing metadata (legacy path / external signals) pass through — the guard never fabricates a ceiling from nothing. This is a defensive backstop, not the primary sizing mechanism: it catches upstream bugs where quantity calculation drifts away from the coordinator's intent.

4. **(D031D) Oversized-held-position detection.** `held_positions_from_portfolio` now accepts `nav` and `max_position_pct`. When provided, each held position's live notional is compared against the ceiling and the result is written to metadata as `position_above_target_ratio` + `oversized_position_flag` (`True` when `ratio > oversize_flag_ratio`, default 1.25×). Detection only — no auto-liquidation. The flag is available for the dashboard to surface "this position is larger than it should be" warnings and for a future remediation task.

5. **(D031E) Stop-loss framework scaffold.** A new module `risk/stop_loss.py` exposes `evaluate_stop_loss(...) → StopLossDecision`, a pure function that given a position and NAV decides whether the position's per-trade loss budget (`nav * max_loss_per_trade_pct`) has been breached. Supports ATR-based structural stops when strategy metadata carries `stop_loss_atr` + (`atr` or `atr_pct`). Not yet wired to a runtime task — `risk/engine.py::_check_max_loss_per_trade_pct` remains the pre-open gate, and the post-open monitor is a scheduled follow-up. The scaffold is intentionally limited to freeze the decision logic so the wiring task cannot silently regress it.

**Reason:** On 2026-04-22 the operator asked "Why are we opening positions that go down and negatively effect our capital?" Investigation of two losing positions (COHR -£3,454, FCOM -£59) showed strategy metadata asked for £7,913 and £750 respectively, while the actual fills were £57,920 and £10,010 — over-sized by 7.3× and 13.3×. That over-sizing, not bad signals, was the mechanical cause of the adverse P&L magnitudes: at the strategy's intended sizing COHR's -2.9% drift would have cost ~£230 (not £3,454) and FCOM's -0.6% would have cost ~£4.50 (not £59). The bug violated the architecture's stated principle that sizing is a computed output respecting strategy intent (D015), not a uniform fixed slug applied by the allocator.

**Rejected alternatives:**
- *Patch the symptom at `coordinator_action_to_raw_signal`* (overwrite `target_notional` there). Rejected — moves the fix downstream of where the wrong size is decided and makes the audit trail lossy.
- *Lower `default_position_pct` to 0.01.* Rejected — shrinks everything uniformly (including legitimate high-conviction momentum breakouts) without restoring conviction-based scaling; also masks the underlying bug rather than fixing it.
- *Drop the hard cap entirely and trust strategies.* Rejected — a misconfigured strategy requesting 50% of NAV should still be clipped. The cap is a cheap safety net.
- *Enforce post-open stops in this same task.* Rejected — stop-loss enforcement is a separate surface (monitor cadence, close-order routing, idempotency) and mixing it into a sizing correction would bloat the blast radius. Scaffold now, wire in a dedicated follow-up.

**Status:** Implemented. Covered by:
- `tests/test_global_edge_coordinator.py` — 7 new cases: `target_notional` honoured, `risk_notional_override` wins, hard-cap clips absurd requests, `nav_fallback` preserved, small sizes never inflated, audit metadata completeness, arbitrage path unchanged, oversized held-position flag.
- `tests/test_execution_engine.py` — 5 new cases for the boundary guard: within-tolerance pass, gross over-sizing reject, hard-cap reject, no-metadata no-op, arbitrage exempt.
- `tests/test_stop_loss_scaffold.py` — 5 new cases for the pure `evaluate_stop_loss` helper: portfolio stop triggers / stays quiet, ATR-pct structural stop, short-position structural stop, invalid-price safe default.
- `system/trading_loop/loop.py` threads `max_position_pct` (read from risk-engine config, default 0.10) into both the opportunity builder and the held-position builder.
- No config schema changes required — all new behaviour derives from existing keys (`max_position_pct` in `config/risk_limits.yaml`).

**Expected behaviour after D031** (on the same £1M NAV, £7,913 COHR breakout, £750 FCOM mean-reversion):
- COHR opens at £7,913 not £57,920. A 1-ATR adverse move (~1.8%) loses ~£142 instead of ~£1,040.
- FCOM opens at £750 not £10,010. A 0.6% adverse drift loses ~£4.50 instead of ~£59.
- A hypothetical mis-configured strategy requesting £500k is clipped to £100k (10% NAV cap) and the clip is logged with reason `nav*0.10`.
- Existing oversized positions are flagged (`oversized_position_flag=True`) on every tick but NOT auto-trimmed; operator or a follow-up remediation task decides how to unwind.

**Important scope limitation: strategies do not yet emit per-signal target notional.**
The D031 audit trail (verified against live DB post-deploy) consistently records `sizing_source=nav_fallback` for directional signals because `strategies/momentum.py` and `strategies/mean_reversion.py` currently emit RawSignal metadata with `atr_pct`, `breakout_strength` etc. but NOT `target_notional` or `risk_notional_override`. The values we saw in earlier signal rows (e.g. COHR `target_notional=7913`, FCOM `target_notional=750`) came from the coordinator's own self-loop: `coordinator_action_to_raw_signal` writes `md["target_notional"] = str(action.capital)`, which on a subsequent accumulator round-trip can appear as if a strategy had "requested" that number. It did not.

Consequence: D031A as implemented strictly follows the brief (respect explicit metadata when present, fall back otherwise) but its *practical* effect on existing strategy sizes is (a) enforce the `nav * max_position_pct` hard cap on the nav-fraction baseline, and (b) make every sizing decision auditable. The *expected* win from "respect strategy intent" only materialises once a strategy actually emits intent — see D032 below.

**Operator follow-ups (next cycle):**
- **D032 — Strategies emit per-signal target notional.** Modify `strategies/momentum.py` and `strategies/mean_reversion.py` (and any other directional strategy) to populate `RawSignal.metadata["target_notional"]` based on the strategy's own conviction (confidence) and volatility (ATR%). Without this, the D031A priority path (step 1/2) never fires. Likely shape: a per-strategy `base_notional_usd` config × confidence scalar × volatility scalar, with the coordinator applying the hard cap on top. This is the change that actually turns D031's plumbing into the 7-13× sizing reduction the user expected.
- Wire `evaluate_stop_loss` into a 5–30 s monitor task in `system/orchestrator.py` (similar to the D029 NAV heartbeat) that closes positions whose loss exceeds budget.
- Surface `oversized_position_flag` on the Positions dashboard panel with a one-click "trim to target" action.
- Add confidence-aware scaling of the strategy target (confidence 0.85 → 1.3×, confidence 0.40 → 0.6×) so that hunter's lower confidence threshold doesn't translate to the same per-trade size as trader's higher bar.
- **Current oversized positions:** on the live paper book at deploy time, COHR sat at ~£118k (≈11% of £1.05M NAV) — marginally above the 10% hard cap — and is now flagged by D031D (`oversized_position_flag=True`). FCOM (£10k ≈ 1% NAV) and FIX (pre-existing IBKR manual position) are within cap. Per D031D semantics no auto-liquidation happens; operator decides whether to trim manually or wait for the D032 stop-loss wiring.

---

## D032 — Strategy emits explicit per-signal target notional

**Date:** 2026-04-22
**Decision:** Directional strategies now emit an explicit absolute `target_notional` in `RawSignal.metadata`, so the D031 sizing priority path (`risk_notional_override` > `target_notional` > `nav_fallback`) can use genuine strategy intent rather than defaulting to NAV fallback.

Implemented in:
- `strategies/momentum.py`
- `strategies/mean_reversion.py`
- `config/strategies.yaml` (`base_target_notional` for both strategies)

Each strategy now computes target size from:
1. `base_target_notional` (default 5000)
2. confidence scale (bounded 0.75x..1.25x)
3. ATR%-based volatility scale (bounded 0.70x..1.30x)
4. final clamp to 0.50x..1.50x of base notional

The emitted metadata fields are:
- `target_notional`
- `sizing_base_notional`
- `sizing_confidence_scale`
- `sizing_volatility_scale`
- `sizing_intent_source=strategy_confidence_volatility`

**Reason:** D031 fixed coordinator/execution plumbing but directional strategies still emitted no explicit target size, causing universal `sizing_source=nav_fallback`. That made the boundary guard protect against oversizing, but did not restore strategy-level notional intent. D032 makes the intent explicit at the source.

**Status:** Implemented and covered by tests:
- `tests/test_strategies.py` now asserts both strategies emit `target_notional` and validates ATR-aware scaling behavior.
- Existing D031/D031C suites remain green.

**Operational impact:** This removes the last structural reason for bulk sizing-guard rejects caused by mismatched intent vs computed quantity. Sizing is now strategy-owned, auditable, and still capped by downstream hard risk ceilings.

---

## D033 — Runtime post-open stop-loss monitor (D031E wired)

**Date:** 2026-04-22
**Decision:** Wire `risk/stop_loss.py::evaluate_stop_loss` into a dedicated orchestrator background task that runs every 5-30s (default 15s) and submits `reduce_only` close signals for positions breaching either:
- portfolio loss budget (`max_loss_per_trade_pct`), or
- structural stop metadata (`stop_loss_atr` + `atr`/`atr_pct`).

Implementation details:
- New orchestrator task lifecycle:
  - starts on `Orchestrator.start()`
  - cancels on `Orchestrator.stop()`
- Close path is **not** a risk bypass:
  - builds a `RiskSignal(strategy="stop_loss_monitor", reduce_only=True)`
  - runs `RiskEngine.evaluate_and_persist(...)`
  - only then routes to `ExecutionEngine.execute(...)`
- Added per-position close cooldown (`STOP_LOSS_CLOSE_COOLDOWN_SEC`, default 60s) to prevent repeated close spam while fills/reconciliation settle.

**Reason:** D031E delivered pure decision logic but left runtime enforcement pending. Without a monitor, `max_loss_per_trade_pct` only gates new entries and does not protect already-open positions intraday.

**Status:** Implemented in `system/orchestrator.py` with tests in `tests/test_stop_loss_monitor.py` (close on breach, no-op when within budget, loop cancellation). Full suite green.

---

## D034 — Strategy expansion wave: event-driven + pairs + volume/flow

**Date:** 2026-04-22
**Decision:** Expand the live strategy roster with three new modules wired into the existing `RawSignal -> SignalEngine -> RiskEngine -> ExecutionEngine` contract:
- `event_driven_news`: creates directional signals from AI/news shock context (`news_scores`, `news_details`, macro confidence).
- `pairs_trading`: creates relative-value signals from configured pair spreads using rolling hedge-ratio spread z-scores.
- `volume_flow`: creates continuation/exhaustion signals from volume anomaly + bar-return behavior.

Integration constraints kept:
- No risk bypasses.
- Strategy sizing intent stays metadata-driven (`target_notional`, scaling diagnostics).
- New strategies are YAML-gated in `config/strategies.yaml` and can be toggled via existing control flows.

**Reason:** The allocator/risk architecture was already mature, but alpha diversity lagged. This adds high-ROI edges (event, relative value, flow) without cross-layer coupling or broker interface changes.

**Status:** Implemented in `system/trading_loop/loop.py`, `strategies/event_driven.py`, `strategies/pairs_trading.py`, `strategies/volume_flow.py`, with coverage in `tests/test_strategies.py`.

---

## D035 — Demand-engine gating + volatility/regime/meta wave

**Date:** 2026-04-22
**Decision:** Introduce a global `DemandEngine` (`system/demand_engine.py`) that computes a bounded demand score from AI news/macro and cross-asset anchor returns, then apply it in three places: (1) strategy emission context (`regime_rotation`), (2) pre-allocation meta-label filtering (`signals/meta_labeler.py`) for candidate/raw signals, and (3) opportunity/coordinator ranking bias (`signals/opportunity_engine.py` demand multiplier + `portfolio/global_edge_coordinator.py` demand-adjusted regime-fit). Add two strategy modules: `volatility_regime` (ATR regime breakout/compression) and `regime_rotation` (risk-on/off proxy rotation).

**Reason:** Move from signal-only architecture to opportunity-driven behavior with an explicit latent-demand variable that influences both candidate quality and allocation ranking across D015 and global-edge paths.

**Status:** Implemented in loop/config/strategies with tests in `tests/test_strategies.py` and `tests/test_demand_meta.py`.

---

## D036 — Wave 3: cross-asset demand graph + volatility overlay + ML-style meta-labeling

**Date:** 2026-04-22
**Decision:** Add an explicit cross-asset demand graph module (`system/cross_asset_demand_graph.py`) and feed it into `DemandEngine` as a first-class component. Upgrade meta-labeling (`signals/meta_labeler.py`) from threshold-only rules to a feature-scored probability gate (sigmoid over confidence, demand alignment, volume/news features, strategy prior bias). Add a portfolio-level volatility overlay in `portfolio/allocation_engine.py` that scales gross exposure target using market-volatility proxies from regime metadata.

**Reason:** Improve robustness in regime transitions by (1) extracting demand from structured cross-asset relationships, (2) selecting higher-quality candidates probabilistically rather than by hard cuts, and (3) reducing capital deployment during volatility shocks at allocator level.

**Status:** Implemented with coverage in `tests/test_demand_meta.py` and `tests/test_allocation_vol_overlay.py`.

---

## D037 — Wave 4: demand telemetry + mode-calibrated meta-labeling + demand urgency planning

**Date:** 2026-04-22
**Decision:** Extend demand-awareness into observability and execution: (1) publish demand telemetry (`score`, `trend`, confidence, cross-asset components) via runner heartbeat and dashboard snapshots (`d015` and `global_edge` payloads), (2) support per-profile-mode calibration in meta-labeling (`defender/trader/hunter` probability thresholds), and (3) apply demand-conditioned urgency multipliers in `execution/planner.py` so aligned opens/increases are prioritized while countertrend expansion is de-prioritized.

**Reason:** Keep allocator/execution behavior aligned with the same latent demand state and make that state visible to operators in both control-tower and heartbeat channels.

**Status:** Implemented in `system/trading_loop/loop.py`, `system/dashboard_publish.py`, `signals/meta_labeler.py`, `execution/planner.py`, with tests in `tests/test_demand_meta.py` and `tests/test_execution_planner_demand_urgency.py`.

---

## D038 — Wave 5: demand-aware routing + adaptive meta priors + demand alerts

**Date:** 2026-04-22
**Decision:** Extend demand-awareness to execution venue selection and online strategy priors: `SmartOrderRouter.route(...)` now accepts optional metadata and applies demand/profile-aware venue preference for crypto/equity paths; trading loop periodically computes dynamic strategy bias from recent order outcomes (`signals/meta_adaptation.py`) and merges it into meta-label strategy priors; demand regime-shift alerts are emitted into heartbeat and dashboard snapshot payloads.

**Reason:** Improve realized execution quality and adapt signal acceptance to live fill behavior without bypassing risk or changing frozen broker interfaces.

**Status:** Implemented in `execution/router.py`, `system/trading_loop/loop.py`, `signals/meta_adaptation.py`, `system/dashboard_publish.py`, with tests in `tests/test_meta_adaptation.py` and `tests/test_router_demand_bias.py`.

---

## D039 — Wave 6: learned routing feedback + mode-adaptive demand thresholds + UI diagnostics

**Date:** 2026-04-22
**Decision:** Add a lightweight learned routing-quality map in `SmartOrderRouter` keyed by `(broker, symbol)`, updated from realized execution feedback (filled vs non-filled and slippage proxy) to influence future broker ranking. Extend demand-alert gating with per-mode thresholds (`defender/trader/hunter`) and persist short alert history in heartbeat/snapshot payloads. Surface demand/meta diagnostics in redesign Risk screen from runtime heartbeat and dashboard snapshot.

**Reason:** Close the loop between execution outcomes and routing choice, adapt demand sensitivity to operating mode, and make latent-state adaptation observable to the operator.

**Status:** Implemented in `execution/router.py`, `system/trading_loop/loop.py`, `config/strategies.yaml`, `ui/src/app/redesign/useLiveSystem.ts`, `ui/src/app/redesign/screens.tsx`, with tests in `tests/test_router_demand_bias.py` and `tests/test_demand_meta.py`.

---

## D040 — Wave 7: persistent routing trajectories + mode-adaptive alerts + diagnostics endpoint

**Date:** 2026-04-22
**Decision:** Persist learned routing quality state (`quality_map` + per-symbol short history) into control-state (`routing.quality.state`) and reload it on loop startup; apply configurable decay policy each N iterations to prevent stale overfit. Extend demand alerts with mode-aware thresholds and publish alert history in heartbeat/snapshot payloads. Add dedicated diagnostics API endpoint `/diagnostics/routing-quality` and wire redesign UI to show routing trajectories alongside demand/meta diagnostics.

**Reason:** Make routing learning durable across restarts, keep signal/reactivity mode-consistent, and give operators direct visibility into execution-learning dynamics.

**Status:** Implemented in `execution/router.py`, `system/trading_loop/loop.py`, `api/server.py`, `ui/src/app/redesign/useLiveSystem.ts`, `ui/src/app/redesign/screens.tsx`, with tests in `tests/test_router_demand_bias.py` and `tests/test_api_dashboard_extras.py`.

---

## D041 — Wave 8: routing confidence intervals + adaptive decay + trajectory sparklines

**Date:** 2026-04-22
**Decision:** Extend routing learning with broker-symbol confidence diagnostics and adaptive decay mechanics: routing export now includes per-broker sample stats (`n`, `std`, `ci95_half`) and decay can adapt to observation count, turnover/liquidity EMA proxies, and staleness. Trading loop now passes turnover/liquidity hints into routing feedback. Diagnostics payload/type expanded and redesign Risk diagnostics render per-symbol trajectory sparklines with CI95.

**Reason:** Raw point scores are insufficient for operator trust and can overfit stale sparse samples; confidence-aware routing telemetry and adaptive forgetting make the learning loop safer and more interpretable.

**Status:** Implemented in `execution/router.py`, `system/trading_loop/loop.py`, `ui/src/app/lib/api.ts`, `ui/src/app/redesign/screens.tsx`, with tests updated in `tests/test_router_demand_bias.py`.

---

## D042 — Wave 9: fee-prior fusion + slippage percentiles + broker comparison diagnostics

**Date:** 2026-04-22
**Decision:** Extend `SmartOrderRouter` with (1) a Bayesian-style **fused routing score** that blends a fee-derived prior (`ROUTING_PRIOR_PSEUDO_N` pseudo-observations) with online learned quality using observation count `n`, used as the secondary sort key after explicit fee; (2) persistent per-(broker, symbol) **execution sidecar** metrics: rolling absolute slippage samples (bounded window) with exported **p50/p90 slippage bps** and **fill rate**; (3) structured export fields `broker_comparison` and `exec_metrics` alongside existing `quality_map` / `quality_stats` / `history` in `routing.quality.state`; (4) `/diagnostics/routing-quality` returns the full persisted blob including `quality_stats` (previously omitted); (5) redesign Risk diagnostics **broker comparison table** plus fused-aware “best venue” selection for the trajectory column.

**Reason:** Sparse feedback should not dominate venue choice until evidence accumulates; operators need comparable slippage tail risk and fill reliability next to CI-aware scores; the diagnostics API should mirror what the loop persists so the UI and external clients stay consistent.

**Status:** Implemented in `execution/router.py`, `api/server.py`, `ui/src/app/lib/api.ts`, `ui/src/app/redesign/screens.tsx`, with tests in `tests/test_router_wave9.py` plus updates to `tests/test_router_demand_bias.py` and `tests/test_api_dashboard_extras.py`.

---

## D043 — Strategy mix: default roster + live intelligence sparklines

**Date:** 2026-04-22
**Decision:** The redesign Strategy mix grid always seeds the canonical signal + arbitrage roster (matching `TradingLoop` / `config/strategies.yaml`) via `DEFAULT_STRATEGY_MIX_ROSTER` in `ui/src/app/redesign/mapping.ts`, merged with `loaded_strategies` and allocator snapshot weights. Per-strategy sparklines use recent confidences from `/intelligence/signals` (`intelligenceSparkForStrategy`); allocator-active rows without a DB trace use a lightweight synthetic series from mix weight. `GET /system/status` responses without `loaded_strategies` clear client roster state so off-mode does not show stale loop registrations.

**Reason:** Operators saw an empty Strategy mix before the first allocator publish; the taxonomy roster and signal history should be visible whenever the dashboard loads.

**Status:** Implemented in `ui/src/app/redesign/mapping.ts`, `ui/src/app/redesign/screens.tsx`, `ui/src/app/redesign/data.ts`, `ui/src/app/redesign/useLiveSystem.ts`.

---

## D044 — Dashboard capital allocation: hybrid slider (raise commits · lower stages)

**Date:** 2026-04-23
**Decision:** The redesign dashboard grows a dedicated, full-width **Capital allocation** panel (`ui/src/app/redesign/capital.tsx`) mounted between the NAV hero and the conviction/live-feed row. Interaction is asymmetric by design:

- **Dragging up past the deployed line commits the ceiling on release** via `PUT /system/capital-allocation` (`live.setCapitalPct`) — new-position headroom expansions are low-risk and don't warrant a confirm step.
- **Dragging below the deployed line stages a trim** with a weakest-first preview (ascending unrealised P&L as the hold-score proxy), a per-symbol protect list, and an explicit *Confirm* that lowers the ceiling. The preview is honest: the engine unwinds on its own signals — this is **not** a force-close. Per-symbol close lives in Book.
- **Dragging below `FLATTEN_THRESHOLD` (3%) opens the flatten confirm** with a 1.2s hold-to-confirm. Until `POST /positions/flatten` ships, confirm lowers the ceiling to 0% (prevents new deploys) and surfaces a "backend pending" banner. No fake success.

`useLiveSystem.setCapitalPct` was hardened to **revert optimistic local state on failure** and to adopt the server-confirmed value when the backend clamps or rejects; `CapitalPanel` relies on this contract to suppress the "committed" banner when the PUT never took effect. Three keyframes were added to `src/styles/design-system.css`: `ds-tick-flash` (deployed-line crossing), `ds-danger-pulse` (flatten thumb halo), `ds-slide-up` (confirm/result banners). The panel is always mounted — even when the system is off — so the ceiling can be pre-set and will be honoured on next start.

The Kill Switch control that ships alongside the slider in the design bundle was deliberately **not** wired: the scope was the slider only, and the top-bar Power control already provides the graceful halt path.

**Reason:** Operators need a single, discoverable surface for "raise the cap now" and "reduce my book" that (a) distinguishes the safe case (raise headroom, commit immediately) from the consequential case (trim or flatten, explicit confirm with preview) and (b) is truthful about which backend actions are available today — a mis-labelled "flatten" button that silently lowers the ceiling would erode trust. The ledger contract for `PUT /system/capital-allocation` (idempotent, clamped, returns confirmed value) already supports the UI's optimistic-with-revert model.

**Status:** Implemented in `ui/src/app/redesign/capital.tsx` (new), `ui/src/app/redesign/dashboard.tsx` (mount + grid row), `ui/src/app/redesign/useLiveSystem.ts` (hardened `setCapitalPct`), `ui/src/styles/design-system.css` (three new keyframes). Sourced from `ui/newui/project/prototypes/redesign_capital_port/capital.tsx`; `KillSwitchButton` and inlined `CAPITAL_KEYFRAMES` export deliberately omitted.

### D044.1 — Gauge on *capital at work*, not positions-only

Follow-up to the initial D044 shipment: the slider originally drew its landmark line and computed "free to deploy" against **filled positions only**, while the Book screen's *Capital at work* card showed **positions + pending orders**. Operators saw two different percentages for the same underlying book (e.g. 46.3% on the slider vs 49.2% in the Book card) — a ~3pp gap that is exactly the reserved notional of unfilled orders.

The backend's `cap_slider` gates `deploy = NAV × ge × cap_slider` in `portfolio/allocation_engine.py`, and *deploy* in that context covers both new positions AND the buy orders feeding them (because a pending order has already consumed allocator budget). The slider must therefore gauge against **capital at work = filled positions + pending-order notional**, or the gauge is dishonest: the snap landmark sits in the wrong place, "free to deploy" overstates headroom by the pending amount, and the "raise the ceiling to match" mental model doesn't match what the allocator actually does.

Fix landed as a single shared helper `capitalAtWork(positions, orders)` in `ui/src/app/redesign/mapping.ts` (alongside `isPendingOrderStatus` / `pendingOrderNotional`). Both surfaces — the dashboard slider and the Book screen — consume this helper, so the two can never drift again. In the slider, the tick label changes from `deployed · N%` to `at work · N%`; `IdleInfo` now shows **At work** as the headline row with an optional `positions £X / pending £Y` sub-row when pending is materially non-zero. `computeTrim` also shifts its `mustRelease` baseline to `workingValue` so the over-commitment figure honestly includes pending-order notional — the close list remains positions-only (pending orders unwind via cancel / engine signals, not via this UI), and the existing shortage banner already narrates that gap.

Duplicated `isPendingOrder` / `toFiniteNumber` in `screens.tsx` were removed in favour of the shared helpers. No backend changes.

---

## D045 — Trading-engine audit: sizing guards, crypto venue routing, and trim exits

**Date:** 2026-04-25
**Decision:** Fix the global-edge/D015 execution path so missing feature prices never fall back to `1`, and global-edge opportunities carry the resolved `close`/`price` and `side` metadata into `SignalEngine`. Route spot crypto to dedicated crypto venues ahead of Alpaca unless explicitly opted in with `allow_alpaca_crypto`; fiat `*-USD` pairs are pinned to Kraken unless explicitly opted into stablecoin conversion, while USDT/USDC-style symbols can use Binance/Bybit. Enable global-edge replacement exits by emitting reduce-only `trim_symbol` actions for displaced held positions; close/trim actions preserve the position's broker and still pass through the normal risk and execution engines.

**Reason:** The audit found repeated broker rejections caused by target notional being interpreted as coin/share quantity (for example ~55k BTC/ETH/XRP units) when price metadata was missing. It also found Alpaca being selected for crypto because of the zero-fee prior despite no Alpaca USD buying power, and found the global-edge path ranking held positions without emitting any close/trim action, which left profitable or weak holdings unrealisable except by stop-loss/manual intervention.

**Status:** Implemented in `system/trading_loop/loop.py`, `portfolio/global_edge_coordinator.py`, `signals/arb_bridge.py`, `execution/d015_instruction_executor.py`, `execution/router.py`, and `config/global_edge.yaml`, with regression coverage in `tests/test_global_edge_coordinator.py`, `tests/test_d015_instruction_executor.py`, and `tests/test_router_demand_bias.py`. Live verification after restart showed a normal-sized `AMTM` reduce-only sell trim and no new 55k-unit crypto orders; IBKR was excluded during verification because Gateway/TWS was in an API zombie state.

---

## D046 — Kraken/Binance/Bybit paper book + global-edge churn guard

**Date:** 2026-04-26
**Decision:** (1) **Position reconciliation** — adapters in `_NO_NATIVE_PAPER_POSITION_BROKERS` (`kraken`, `binance`, `bybit`) do not host exchange-native paper positions. In `paper_mode`, reconciliation must treat the latest synthetic `PositionLog` snapshot for that broker as authoritative instead of replacing it with the live account’s empty spot book (which made the allocator think the book was flat after every fill). When merging per-broker latest rows into the local quantity map, skip double-counting rows already included in the global latest-timestamp snapshot (`b_ts == latest_ts`). (2) **Global-edge coordinator** — skip emitting `open_strategy` when an opportunity’s symbol and **side** already match a held position (instant paper fills made “open same leg every loop” too easy). Opposite-side opportunities are not blocked so trims/flips can still flow through risk and execution.

**Reason:** Operators saw repetitive Telegram `PAPER OPEN FILLED` lines with identical size/price: real synthetic orders were logged, but reconciliation cleared paper crypto legs while strategies kept re-proposing the same edge; the coordinator could also add the same-direction leg again because trim selection skips same-symbol displacement.

**Status:** Implemented in `execution/engine.py`, `core/broker_paper.py` (shared broker set), `portfolio/global_edge_coordinator.py`, and `GET /positions` now merges the latest `PositionLog` rows for `kraken` / `binance` / `bybit` whenever `APP_ENV` is not `live` and live adapters returned at least one row (`source=live_broker+synthetic_paper_log`), so the Book matches the persisted synthetic crypto leg. Tests in `tests/test_global_edge_coordinator.py`.

---

## D047 — Broker reconnect readiness bypass + immediate late-broker ingestion

**Date:** 2026-04-27
**Decision:** Keep the broker reconnect loop as the single retry mechanism for every configured registered broker, but make IBKR readiness edge-triggered: a cheap TCP/API probe runs while disconnected, and a transition from not-ready to ready bypasses any outstanding exponential full-connect backoff. The probe does not increment the full-connect failure counter, so a closed TWS/Gateway cannot push reconnects into multi-minute delay before the operator launches it. Separately, `TradingLoop` now runs a lightweight `broker-join-poll` (`BROKER_JOIN_POLL_SEC`, default 2s) so newly connected adapters are added to the router and execution engine without waiting for the next full strategy iteration.

**Reason:** IBKR/TWS is locally operated and frequently starts after the trading system. A prior failed full connect could leave IBKR offline until a long backoff expired, and even after connection the trading loop could take up to the strategy cadence to include it. The system should pick up a launched/restarted API client within the next health/join polls while still respecting backoff for expensive remote exchange auth retries.

**Status:** Implemented in `system/broker_manager.py`, `system/trading_loop/loop.py`, with regression coverage in `tests/test_broker_balance_ready.py`.

**Operational note:** IB Gateway can show "API Server connected" while refusing all third-party clients until the paper-trading disclaimer is accepted. Gateway logs this as "Paper trading disclaimer must first be accepted for API connection" and the UI row remains "API Client disconnected". Broker status now points operators at that prompt before treating the condition as a generic zombie/restart case.

---

## D049 — Strategy dashboard roster hygiene and advanced sleeve cards

**Date:** 2026-04-27
**Decision:** Keep the redesign Strategy screen as an alpha-strategy roster, not a catch-all runtime event list. `DEFAULT_STRATEGY_MIX_ROSTER` now includes the advanced strategy families that belong in this view (`factor_sleeve`, `stat_arb_pairs`, `options_long_call`, `options_long_put`, `options_protective_put`, `options_covered_call`) so disabled or idle sleeves are visible as explicit cards. Internal allocator maintenance actions (`global_edge_trim`, `trim_symbol`) are filtered out of strategy mapping and mix rows; they belong in allocation, portfolio-rotation, and execution observability, not the strategy taxonomy.

**Reason:** Operators need to see which strategy sleeves exist, which are disabled, and which are idle. Showing `global_edge_trim` as a strategy made the Strategy screen misleading because it is an allocator/execution action generated by D015 replacement logic, not a source of alpha.

**Status:** Implemented in `ui/src/app/redesign/mapping.ts` and `ui/src/app/redesign/screens.tsx`, with guard coverage in `tests/test_ui_strategy_roster.py`. Focused pytest checks and the Vite production build pass.

### D049.1 — Paper mode should exercise the advanced strategy sleeves

Follow-up: the Strategy dashboard is used during paper mode, so advanced
strategy sleeves should not appear as disabled merely because they are not yet
production-approved. `config/factor_sleeve.yaml`, `config/pairs_trading.yaml`,
and `config/options_strategies.yaml` now ship enabled for paper observability;
options remain `paper_only: true` and still pass through the options risk policy.
The redesign fallback roster mirrors that stance so a fresh dashboard render does
not show pink disabled badges before `/system/status` catches up.

`Idle` still has meaning: the sleeve can be enabled and evaluated but have no
setup, no eligible holding, no option proposal/chain data, no qualifying pair, or
no recent feature window. That is different from a config-disabled strategy and
is the correct paper-mode distinction.

---

## D051 — Correlation-aware Universe Intelligence Layer

**Date:** 2026-04-27
**Decision:** Add a standalone `universe/` package that compresses a large
candidate world into non-redundant tiers for the existing dynamic data/trading
loop. The layer performs eligibility filtering, return/correlation graph
construction, factor-similarity blending, connected-component clustering,
representative selection, and temporary promotion of cold instruments on
anomalies. It writes the existing compact contract
`data/runtime/universe_tiers.json` plus richer research metadata in
`data/runtime/universe_intelligence.json`.

**Reason:** Breadth should be measured as independent opportunity coverage per
unit of compute, not raw instrument count. Correlated instruments should not be
deleted: one or more representatives cover directional exposure, while cluster
members remain available for relative-value and event-driven promotion.

**Status:** Implemented in `universe/`, `config/universe_selection.yaml`,
`scripts/build_universe_tiers.py`, and `docs/UNIVERSE_INTELLIGENCE.md`, with
coverage in `tests/test_universe_intelligence.py`. No risk/execution path was
changed.

---

## D061 — Adaptive sizing rewrite (supersedes D015 / D030 / D031 / D032)

**Date:** 2026-04-30
**Decision:** Remove discretionary hard-coded numerical knobs from the sizing
pipeline (per-strategy notionals, integer action caps, per-action notional
fraction, flat per-position percentage ceiling). Replace them with a single
adaptive coordinator path that:

1. Filters opportunities by the existing displacement gate
   (`expected_edge > weakest_held_edge + edge_advantage(mode)`) and the
   churn / already-held / dedup rules — unchanged from D015.
2. Allocates capital across qualifying opportunities via softmax weights
   `w_i ∝ exp(λ_eff · priority_score_i)` where
   `λ_eff = softmax_lambda · concentration_exponent(mode)`.
3. Sizes each `CoordinatorAction` as `gross_target_capital * w_i` where
   `gross_target_capital = tradable_capital * gross_fraction(mode)` and
   `tradable_capital = NAV * capital_pct` is the operator's slider.
4. Enforces no fixed integer cap on the number of emitted opens — Hunter
   may emit 1 (winner-take-all) or 50 (broad book) depending purely on how
   many opps clear the displacement gate.
5. Reads per-position / concentration / asset-class ceilings from
   `config/risk_limits.yaml::mode_overrides[active_mode]` so Hunter can
   take 100% of the deployable sleeve in one symbol when the edge
   dominates; Defender keeps 20% / Trader 40%.
6. Cancels working orders and forces a fresh plan on the next tick whenever
   the operator moves the capital slider (orchestrator publishes
   `capital_allocation_changed`; loop drains working orders before
   re-running the iteration body against the new `tradable`).

**Reason:** Operator intent: "no hard-coded values — the slider is the only
operator-set capital cap; size, count, concentration, cadence emerge from
market state". Pre-D061 the system was bottlenecked by:

* `base_target_notional: 5000` (and per-mode 25k) on every strategy, which
  caused tiny $5–7k positions even when NAV was $1.07M.
* `max_position_pct: 0.10` flat cap, preventing Hunter from concentrating
  on a dominating opportunity.
* `max_actions_per_tick.hunter: 20` integer cap forcing the coordinator to
  slice gross target into N equal slots even when one opp deserved most.
* Static `liquidity_score=0.7 / execution_score=0.75 / risk_cost=0.05`
  literals in `signal_candidate_to_strategy_opportunity` flattening the
  priority softmax so concentration could not emerge.

**Status:** Implemented behind `USE_ADAPTIVE_SIZING=1` so the legacy path
remains intact. Touched files:

* `portfolio/global_edge_coordinator.py` — `_adaptive_priority_components`,
  `_propose_actions_adaptive`, env-flag gating; static stubs replaced at
  all four call sites (directional + 3 arb wrappers).
* `system/trading_loop/loop.py` — adaptive kwargs passed to
  `propose_actions`; per-mode `max_position_pct` from `mode_overrides`;
  slider event handler cancels working orders before next iter.
* `system/trading_loop/helpers.py` — `apply_saved_mode_to_risk_cfg`
  applies `mode_overrides` from `risk_limits.yaml`.
* `system/trading_loop/loop.py` `_capital_change_pending` flag plumbed via
  `request_iteration("capital_allocation_changed")`.
* `config/risk_limits.yaml` — `mode_overrides` block (defender / trader /
  hunter ceilings; hunter = 1.00 across the board).
* Tests: `tests/test_adaptive_sizing.py` (12 cases — priority components,
  no-integer-cap, dominant-opportunity-100%, capital-sums-to-target,
  legacy-fallback, audit metadata, already-held skip).

**Supersedes:** D015 (allocator) — concentration is now softmax-driven, no
fixed `max_actions_per_tick`. D030 (mode-aware capital fraction) — the
per-mode `max_notional_fraction_per_action` is bypassed; `gross_fraction(mode)`
applied at the loop / coordinator boundary. D031 (respect strategy sizing) —
strategies' `target_notional` is no longer the source of truth; the coordinator
sizes from gross_target × softmax_weight. D032 (per-signal `target_notional`
field) — the metadata field is preserved for audit but not used for sizing
decisions in the adaptive path.

**Migration:** When `USE_ADAPTIVE_SIZING` is unset (default), every legacy
behaviour is preserved bit-for-bit (verified: 50 pre-D061 tests pass
unchanged). To enable, set `USE_ADAPTIVE_SIZING=1` before launching
`python run.py`. To disable mid-flight, unset and restart the Python
process — `/system/stop` + `/system/start` does not reload module-level
imports.

## D106 — Two-agent accounting collision: restored proven state (2026-05-18)
A second agent (Cursor) made overlapping, uncommitted changes to the
accounting subsystem while this session's fixes were in flight:
`system/paper_nav.py` (compounded-paper-NAV model), a `run_m3.py`
rewrite that replaced `_compute_today_realised_pnl`, an `/pnl` NAV-path
swap, and it ran `scripts/backfill_daily_pnl.py` which **recomputed**
pre-2026-05-13 `daily_pnl` from the buggy-fee-era orders — directly
overwriting the operator's explicit, recorded decision (D-prior:
"flag pre-instrumentation as non-production and ZERO it"; recompute was
explicitly rejected).

Resolution (operator-chosen): keep ONE coherent, tested model.
- Cursor's uncommitted work preserved in `git stash@{0}` (recoverable,
  not adopted) — working tree restored to committed HEAD `630b337`.
- DB rectification re-applied (`scripts/rectify_daily_pnl.py --apply`):
  pre-05-13 re-zeroed; all-time realised = valid post-instrumentation
  days only. Cursor's recomputed state also backed up.
- Cherry-picked Cursor's two correct, non-conflicting fixes only:
  `trade_count` now counts filled `OrderLog` rows (was `SignalLog`,
  ~2x overcount); UI "Trades today" → "Fills today".
- NAV remains the synthetic-crypto-wallet + single-source `/pnl` model
  (`04d736a`/`6b3fd56`), NOT the compounded-paper model.

Governance note: autonomous agents must not run DB-mutating backfills
that reverse a recorded operator data decision without re-confirmation.

## D107 — Connect Hub plugin fabric and capability registry (2026-05-19)

myTbot now has a first Connect Hub foundation for adaptive onboarding across
four external dependency classes: brokers, information feeds, AI providers, and
treasury accounts.

Decision:
- External systems are declared in non-secret connector manifests
  (`config/connectors.yaml`) with category, auth type, required environment
  variables, roles, capabilities, and safety constraints.
- The runtime exposes a read-only Connect Hub snapshot via `GET /connect/hub`
  and embeds the same payload in `GET /system/status` as `connect_hub`.
- The redesign UI has a Connect screen that renders the same adaptive
  connector inventory, including next actions such as missing env vars,
  pipeline runs, broker start, or treasury approval requirements.
- Connector cards expose a guarded Configure wizard that writes declared
  credential env vars to `.env`, never echoes secret values, and can enable the
  connector/provider configuration where applicable.
- The snapshot adapts to the current user setup: broker rows merge live
  `BrokerManager` status, information-feed rows merge ingest telemetry,
  AI-provider rows merge `config/ai.yaml`, and treasury rows remain disabled
  unless explicitly configured.
- Secret values are never returned. The API reports only whether each required
  environment variable is configured.
- Treasury movement is deliberately metadata-only at this stage. A connector may
  declare future capabilities, but automatic transfer execution remains disabled
  by policy and requires a later approval workflow before any cash movement code
  exists.

Reason:
The one-button core must not assume that every operator has the same broker,
news stack, AI stack, or treasury account. The system should scale down to one
broker/no treasury/no paid AI and scale up to multiple venues, feeds, local and
paid LLMs, and governed treasury funding without changing allocator, risk, or
execution logic.

Status:
Implemented in `system/connect_hub.py`, `connectors/base.py`,
`config/connectors.yaml`, `api/server.py`, `docs/CONNECT_HUB.md`, redesign UI
route/screen files, and UI API types in `ui/src/app/lib/api.ts`, with focused coverage in
`tests/test_connect_hub.py`, `tests/test_connector_contracts.py`, and
`tests/test_api_dashboard_extras.py`. This is a read-only/onboarding inventory
slice; OAuth flows, generic unknown-protocol adapters, and treasury execution
are future work.

## D107 — Broker/market operating hours as a first-class decision input (2026-05-19)
Previously the market-session gate (`core/market_session.is_market_open`)
acted ONLY at the execution last-mile: the strategy/allocator selected
positions blind to venue hours, then orders bounced at `execute()`. This
wasted cycles, distorted allocation (un-tradeable names chosen over
tradeable ones), and made the harvest/stop monitors re-attempt closed-
market closes every cycle (the pre-market "389× did not execute" spam;
winners sat undefended).

Now broker/market hours are a first-class decision input:
- `config/market_hours.yaml` — declarative per-venue session policy
  (`always` for 24/7 crypto venues; `by_asset_class` otherwise).
- `core/market_session.is_tradeable(broker, asset_class, symbol)` — the
  single broker-aware authority. The proven `is_market_open` asset-class
  gate is left byte-identical (foundation is purely additive).
- Wired upstream: (a) the D015 allocator drops closed-venue opportunities
  BEFORE allocation; (b) profit-harvest / stop-loss / aggregate-de-risk
  monitors skip closed-venue positions quietly (DEBUG) instead of
  spamming failed closes — they re-evaluate automatically at reopen;
  (c) the `execute()` gate upgraded to the same broker-aware authority
  (defence-in-depth, unchanged role).

Honest scope: this is an efficiency/correctness/clarity change (capital
only allocated to currently-tradeable instruments; no pre-market spam),
NOT a profitability change. `MARKET_SESSION_GATE=0` disables; absent/
invalid YAML → built-in defaults (backward-safe).

## D131 — Crypto cluster cap + disable bleeding alphas (2026-05-23)

**Decision:** Add a venue-spanning crypto cluster cap (analogous to the
D115 fx + equity-index cluster caps) and disable the two consistently-
losing alpha strategies (`volatility_regime`, `volume_flow`).

**Why.** The dashboard showed `-$9,805` unrealised, `-$528` realised
overnight, 18.9 % win rate, 0.50 profit factor after only 15 hours of
post-reset soak. Deep-dive via `/performance`:

* 96 % of the unrealised loss was **stacked long crypto** across all
  three venues: kraken BTC $60k / kraken XRP $61k / binance AVAX $60k /
  binance AAVE $42k / binance ETH $28k = **$251k one-direction long
  crypto on $1.21M NAV (≈ 21 %)**. Each leg sat under the 5 %
  single-name cap but together formed one correlated crypto-beta bet.
  Crypto dropped 3 – 5 % overnight and they all lost together. There
  was no crypto cluster cap.
* **`volatility_regime`** booked 60 closes / **1 win** / **−$1,740**
  (stacking shorts on the same crypto names that mean_reversion was
  stacking longs — the system fighting itself).
* **`volume_flow`** booked 4 closes / 0 wins / −$116.

**Crypto cluster cap.** New `_check_crypto_cluster_exposure` in
`risk/engine.py`, wired into the standard check list right after the
fx + equity-index cluster checks. Bounds `|sum(signed_qty *
current_price) for positions where asset_class=='crypto' or symbol
endswith '-USD'|` to `max_net_exposure_pct` of NAV (default `0.10` =
10 %). Reduce-only never blocked; neutralising legs that REDUCE the
absolute cluster magnitude always pass. Config:
`config/risk_limits.yaml::crypto_cluster`.

**Strategy disables.** Posted `POST /strategy/volatility_regime/toggle
{enabled:false}` and same for `volume_flow`; persisted under
`control_state.strategy.enabled.*`, survives restart.

**No forced flatten.** Manual flatten would lock the −$9.3k crypto
unrealised into realised loss. The new cap prevents *new* accumulation;
`intraday_derisk_monitor` can grind the existing concentration down
under reduce-only flow when it triggers, and a crypto rebound is still
possible. The right tradeoff is "stop digging", not "panic-sell the
bottom".

**Tests.** `tests/test_d131_crypto_cluster.py` — 7 tests (non-crypto
skip, within-cap pass, cross-venue aggregation rejected,
neutralising-leg allowed, reduce-only never blocked, disabled-config
pass-through, `-USD` symbol detection without `asset_class`). Cluster
+ D125 regression: 43 passed.

---

## D130.1 — Second full trading-data reset to a clean slate (2026-05-22)

**Decision:** Operator-requested full wipe of the trading ledger and a
restart, so all data is clean and consistent from a single T0 — this
time with slippage capture (D130) live from the first fill.

**Why now.** D130 added per-fill slippage capture but, by design, it
cannot be backfilled — the 615 fills already booked carry no intended
price. Rather than run a soak on a ledger that is part slippage-aware
and part not, the operator chose to reset and start the soak fresh so
*every* fill from T0 carries `intended_price` + `slippage_bps`.

**Sequence.** `POST /system/stop` → kill `run.py` (releases all broker
connections) → `scripts/reset_trading_data.py --execute` → relaunch
`python run.py` → re-anchor `paper.nav_seed`.

**Flatten semantics.** The operator asked for positions to be flattened
before the wipe. Investigation confirmed `EXECUTION_PAPER_USE_BROKER_ORDERS`
is unset, so the execution engine simulates every paper fill locally and
never places orders at IBKR/Alpaca; reconciliation in paper mode treats
`PositionLog` as authoritative and skips broker `get_positions()`
entirely (`execution/engine.py` ~L2466). All 55 open positions therefore
existed only in mytbot's DB (`positions`/`fills`) + the crypto
`paper_wallet.json` — there were no broker-side orders to close. The
wipe itself flattens the book; the result is the perfectly consistent
state the operator wanted (empty ledger ⇄ flat book) with **no realised
loss incurred** (nothing was sold at a broker).

**Reset-script fix.** `scripts/reset_trading_data.py` now also deletes
`data/runtime/paper_wallet.json` so the three crypto venues restart from
their seed balance instead of carrying stale venue equity computed
against wiped positions — closing the gap that left crypto wallets
inconsistent after the D126 reset.

**Outcome.** All trading tables truncated (orders 638, positions 34,020,
fills 615, signals 3,700, risk_decisions 4,298, strategy_candidate_log
52,983 → 0). `control_state` reduced to the 4-key operator/config
whitelist. Fresh `nav.opening_snapshot` auto-recorded: total
**$1,224,392.84** (ibkr 977,793.85 / alpaca 96,598.99 / kraken·binance·
bybit 50,000 each). `paper.nav_seed` re-anchored to that total. Restart
loaded the D130 code; first 9 post-restart fills show **100 % slippage
coverage** — forward capture confirmed working.

---

## D130 — Per-fill slippage capture + /performance scorecard (2026-05-22)

**Decision:** Capture execution slippage on every fill from now on, and
expose a fills-based performance scorecard endpoint.

**Context.** The operator asked whether myTbot can fully measure its own
performance from the data it holds. Audit of the post-D126 ledger: 599
fills in a single 7.23h window, one `daily_pnl` row. Two findings — (1)
trade-quality metrics (profit factor, win rate, attribution, turnover,
fees, holding period) *are* computable now but not yet statistically
meaningful; (2) time-series risk metrics (Sharpe, Sortino, max drawdown,
Calmar, CAGR, volatility) need a multi-day daily-return series that does
not exist yet; (3) one genuine, time-sensitive gap — **slippage was
never recorded**, and it cannot be retrofitted onto the 599 fills
already booked. It must start capturing before the soak adds more rows.

**P1 — slippage capture.** Added `intended_price` + `slippage_bps`
(both nullable) to the `FillLog` model / `fills` table; migration
`d130a1b2c3d4` chains from `d127a1b2c3d4` and is column-add idempotent.
`storage/fills_ledger.py::record_fill` takes an `intended_price` arg and
derives signed `slippage_bps` via `_slippage_bps()` — **positive = the
fill was worse than intended (an execution cost), negative = price
improvement**; buy-adverse when fill > intended, sell-adverse when fill
< intended. `ExecutionEngine._persist_fill_to_ledger` passes
`signal.suggested_price` as the intended price. Forward-only by design:
pre-D130 fills carry NULL and are excluded from slippage stats.

**P2 — `/performance` scorecard.** New `api/performance.py` +
`GET /performance?days=N` (`days=0` = all history). Trade-quality block
(profit factor, win rate, avg win/loss, payoff ratio, expectancy,
turnover, fees, net P&L), holding-period and slippage distributions
(mean/p50/p90/worst/best + estimated dollar cost), and attribution by
strategy / broker / asset class / symbol come straight from the fills
ledger and are always present. The `time_series` block computes
Sharpe/Sortino/max-drawdown/Calmar/CAGR/volatility from `daily_pnl` but
returns `status="insufficient_history"` until ≥20 daily rows exist. A
`data_quality` block flags trade metrics as descriptive-only below 200
closing trades. Pure read path — mutates nothing.

**Tests.** `tests/test_d130_slippage_scorecard.py` — 14 tests (slippage
sign math, percentile interpolation, ledger column capture, scorecard
trade-quality / slippage / attribution / holding-period, time-series
insufficient + available paths). Live DB check: 599 fills, 144 closing,
profit factor 4.39, win rate 73.6%, net realised $4,246, slippage
coverage 0% (all fills pre-D130 — forward capture starts now).

---

## D129 — Crypto venue-aware sizing + reservation TTL (2026-05-22)

**Decision:** Two crypto-venue fixes — a TTL on the execution-side room
reservation, and venue-aware crypto sizing in the allocator.

**Context.** A health-check dig into the `EXEC SKIP (venue paper
capital)` lines confirmed they are *not* a bug — the crypto paper
wallets are genuinely small (~$50k each) and Kraken/Binance were full
($184k / $142k gross on $50k wallets; crypto shorts count toward
`gross_mv`). The bound + reroute work correctly. But the investigation
surfaced two real issues.

**P1 — reservation TTL (deadlock guard).** `execution/engine.py`
tracks an in-process per-venue room reservation
(`_crypto_paper_room_reserved`) to bridge sub-cycle paper-wallet
snapshot lag. It reset *only* when the published `venue_deploy_room`
changed. If that snapshot ever froze (heartbeat stall), `room` would
stop changing, the reservation would never reset, and the venue would
be **permanently locked out of all new crypto opens** even after
positions closed. Fix: reset the reservation on a TTL too —
`_CRYPTO_RESERVATION_TTL_SEC = 60s` — since the reservation only ever
bridges a single cycle. A new `_crypto_paper_room_reserved_at`
timestamp drives it.

**P2 — venue-aware crypto sizing.** The D015 allocator
(`global_edge_coordinator`) sized crypto opportunities at a fraction of
*total* NAV (~$61k at the 5% single-name cap), blind to the destination
crypto venue's ~$50k wallet — so the order was clamped/rerouted/skipped
at execution, wasting allocator cycles. Fix: `_crypto_venue_room_budget()`
sums the crypto venues' combined deploy room; the action-emit loop
clamps each crypto opportunity's capital to the remaining pool and
decrements it, skipping cleanly once exhausted. Stamps
`sizing_crypto_venue_clamped` for observability. Returns `None` (no
bound, prior behaviour) when the paper-wallet model is disabled.

**Tests.** `tests/test_d129_crypto_venue_sizing.py` — 8 tests (TTL
reset / persist / room-change reset; budget sum / none / partial;
clamp-decrement semantics). Full suite: 1636 passed, 3 skipped.

---

## D128 — ib_insync market-depth crash fix (2026-05-22)

**Decision:** Monkey-patch `ib_insync.wrapper.Wrapper.updateMktDepthL2`
with a bounds-safe version so a malformed IBKR Level-2 depth update can
never crash the event loop.

**Why.** A health check found `run.py` had crashed four times in 17
minutes (13:07–13:24, `exit=0xFFFFFFFF`). Root cause in the log:

    ib_insync/wrapper.py:921 updateMktDepthL2
      dom[position] = DOMLevel(price, size, marketMaker)
    IndexError: list assignment index out of range

ib_insync's depth handler indexes the DOM list on the `update`
operation (`operation == 1`) with **no bounds check** — the `delete`
branch is guarded (`if position < len(dom)`), `update` is not. IBKR
streams depth updates with `position` indices past the current list
length (partial/unentitled depth feeds, out-of-order messages). The
`IndexError` is raised inside the asyncio socket-read callback;
ib_insync's decoder catches most occurrences (177 logged), but under a
burst it escapes and kills `asyncio.run()` → the whole process.

`get_order_book` in `brokers/ibkr/adapter.py` triggers this — it calls
`reqMktDepth` to snapshot the book (used by the execution slippage
estimate and the microstructure shadow).

**Fix.** `brokers/ibkr/ibinsync_patches.py` — `apply_ibinsync_patches()`
replaces `Wrapper.updateMktDepthL2` with a bounds-safe version:
out-of-range `update` grows the list instead of raising; `insert`
clamps the index; `delete` and unknown-`reqId` are guarded; the whole
handler is wrapped so it can never raise out of the event loop.
In-range semantics are unchanged. Idempotent. Applied at
`brokers/ibkr/adapter.py` import time (right after `util.patchAsyncio()`).
This also removes the 177-line log spam (the spam *was* the caught
errors).

**Tests.** `tests/test_d128_ibinsync_patch.py` — 6 tests (the exact
out-of-range-update crash scenario, in-range insert/update/delete
preserved, out-of-range delete, unknown reqId, negative-index insert).
Full suite: 1628 passed, 3 skipped.

**Note.** This is a vendored-library patch — re-verify if `ib_insync`
is ever upgraded from 0.9.86. The upstream bug may also be fixable by
not subscribing to L2 depth at all (top-of-book L1 would suffice for
the slippage estimate); deferred as a larger change.

---

## D127 — Connect Hub v2 design accepted (2026-05-22)

**Decision:** Accept the Connect Hub v2 design — taking D107's read-only
connector inventory to a full lifecycle feature. Design captured in
`docs/CONNECT_HUB.md` ("Connect Hub v2 — Design (D127)"); implementation
is phased (P1–P7) and not yet started.

**Why.** D107 delivered the onboarding *inventory* slice and explicitly
deferred OAuth flows, capability detection, and treasury execution. The
product needs the full lifecycle: any operator can install the app and
connect only the brokers / feeds / AI models / treasury account they
actually have, from a **curated, tested catalogue** — never arbitrary
connectors that could move money through an unverified adapter.

**Key design commitments.**

- **Curated catalogue only.** Users pick from `config/connectors.yaml`;
  new connectors are added by the myTbot team over time, after testing.
- **Three certification tiers** — Certified (may execute), Experimental
  (may inform only — never trades or moves money), Unsupported (cannot
  be added). Effective capability = manifest-declared ∩ test-detected ∩
  tier-permitted.
- **Four category shapes are distinct:** trading platforms and news
  feeds are collections (add/remove N); the AI pipeline is 4 fixed
  stages (Rules / FinBERT / Local LLM / Premium — configured, never
  added/removed; "disable yes, delete no"); treasury is a singleton
  (0 or 1 — it is the capital source-of-truth).
- **Connector lifecycle state machine** — `not_configured →
  needs_credentials → testing → connected | connected_limited |
  error | disabled | unsupported_in_live`.
- **AI pipeline specifics:** Rules is non-disableable core; FinBERT is
  a version-pinned curated checkpoint updated via the model registry +
  smoke test; Local LLM uses a supported-model catalogue with a
  first-run machine probe, an install/activation compatibility cert
  (JSON-mode + latency + schema tests), and graceful skip on weak
  hardware; Premium LLM is provider-based with a compatibility test
  and only ever advises.
- **Treasury** stays read-only in v1; v2 adds approval-gated transfers
  (manual approval, daily cap, reserve floor, beneficiary whitelist) —
  no tier ever gets silent cash movement.
- **Data model:** `config/connectors.yaml` is the catalogue, `.env`
  holds secrets (never DB, never echoed), a new `connector_state`
  table holds per-install state (enabled, test history, detected
  capabilities, certification tier, model versions, machine probe).
  Single-user-per-install — no multi-tenancy.

**Open decisions** (recorded in the design doc, to resolve before the
relevant phase): local-LLM behaviour on weak machines; whether to ship
the custom-model Experimental escape hatch in v1; FinBERT auto-update
toggle; whether Treasury v2 is in-scope or its own project.

**Status:** Design accepted and documented.

**Phase 1 — landed (2026-05-22).** Per-install connector state + the
lifecycle foundation:
- `storage/models.py::ConnectorState` + migration `d127a1b2c3d4`
  (`connector_state` table — unique on `(category, connector_id)`).
- `connectors/lifecycle.py` — the 8-state machine, legal-transition
  guard, and the pure `resolve_status(StatusInputs)` derivation used by
  the API and snapshot builder.
- `connectors/state_store.py` — upsert / load helpers for the
  per-install state.
- `connectors/capability_probe.py` — `probe_connector` turning a
  connector's *declared* capabilities into *detected* ones; Phase 1
  covers brokers (against live `BrokerManager` status) and information
  feeds (against ingest telemetry). AI/treasury return an explicit
  not-yet-probed result.
- `POST /connect/test` — runs the probe, derives the lifecycle status,
  persists `connector_state`, returns the refreshed hub snapshot.
- `tests/test_d127_connect_hub_v2.py` — 21 tests (state machine, store,
  probe). Full suite: 1562 passed, 3 skipped.

Endpoint note: the design doc sketched `/connect/{category}/{id}/test`;
the implementation uses `POST /connect/test` with a body to match the
existing `/connect/{configure,enable,delete,add}` convention.

**Phase 2 — landed (2026-05-22).** Certification tiers wired into
execution gating + the live-mode guard:
- `connectors.yaml` — `certification` field added to manifests; the 5
  production brokers (ibkr/kraken/binance/bybit/alpaca) marked
  `certified`. `ConnectorManifest` parses it; default `experimental`.
- `connectors/certification.py` — `resolve_tier`, `may_execute`
  (certification + paper-only-in-live guard), and the per-process
  cached `broker_execution_decision` used by the risk gate.
- `RiskEngine._check_broker_certification` — a new hard rail (runs
  right after `_check_broker_disabled`): a trade signal to a broker
  that is not `certified`, or to a paper-only broker while the system
  is live, is REJECTED. Reduce-only exits are exempt (and the
  reduce-only path already filters this gate out). Config:
  `config/risk_limits.yaml::connector_certification.enforce` (default
  true). Fail-open on catalogue-load glitches and on brokers absent
  from the catalogue (no manifest → no adapter → cannot route anyway);
  the genuine risk — an in-catalogue `experimental` broker — is caught.
- `certification` surfaced per-connector in the `/connect/hub` snapshot.
- `tests/test_d127_connect_hub_v2.py` extended to 31 tests. Full
  suite: 1572 passed, 3 skipped.

**Phase 3 — landed (2026-05-22).** The AI pipeline as four managed
stages:
- `connectors/ai_pipeline.py` — `build_ai_pipeline_view` builds the
  fixed Rules → FinBERT → Local LLM → Premium descriptor (escalation
  order, enable state, model/version detail); `can_disable_ai_stage`
  is the single authority for the per-stage disable rules.
- Disable rules enforced in `POST /connect/enable`: Rules is the
  non-disableable core (already via the manifest); FinBERT may be
  disabled only when another *enabled* provider carries the
  `sentiment_classifier` role — today it is the sole sentiment
  provider, so it is locked. Local LLM / Premium are freely
  disableable. No AI stage is ever deletable (they are stages, not
  connectors).
- FinBERT versioning: `config/ai.yaml::providers.fin_sentiment` gains
  `version` (logical label) + `model_revision` (pins the exact
  HuggingFace checkpoint). Surfaced on the stage card. NOTE — the
  design sketched registering FinBERT in `model_registry.yaml`; that
  registry's schema (task/target/validation_method/training_dataset)
  fits trained classifiers, not a pinned pretrained HF model, so the
  version lives in `ai.yaml` instead. The *update mechanism*
  (download new checkpoint → checksum → smoke test → atomic swap) is
  deliberately deferred — it is a HuggingFace-environment-specific
  subsystem; P3 surfaces the version and structures the contract.
- `GET /connect/ai/pipeline` — read-only four-stage descriptor.
- `tests/test_d127_connect_hub_v2.py` extended to 45 tests. Full
  suite: 1579 passed, 3 skipped.

**Phase 4 — landed (2026-05-22).** Local LLM machine probe, catalogue,
install/cert, graceful fallback:
- `connectors/machine_probe.py` — best-effort CPU / RAM (psutil) /
  GPU+VRAM (torch.cuda) / disk / Ollama detection. `psutil` added to
  `requirements.txt`.
- `config/local_llm_catalogue.yaml` — curated supported-model list
  (mistral:7b, qwen2.5:7b, llama3.1:8b, qwen2.5:14b) with per-model
  disk / RAM / VRAM requirements + quality rank.
- `connectors/local_llm.py` — `compute_fitness`
  (recommended/available/too_slow/unsupported per machine),
  `recommend_model`, `resolve_local_llm_availability`,
  `build_local_llm_view`, `cert_local_model` (JSON-mode + schema +
  latency cert), `install_local_model` (`ollama pull` + cert,
  catalogue-only), `set_local_llm_model`, `list_installed_models`.
- Endpoints: `GET /connect/machine-probe`,
  `GET /connect/ai/local/catalogue`,
  `POST /connect/ai/local/install`, `POST /connect/ai/local/activate`.
- `tests/test_d127_connect_hub_v2.py` extended to 52 tests. Full
  suite: 1593 passed, 3 skipped.

Open decisions resolved in P4:
  * **#1 weak machine → silent skip.** A machine that fits no model
    gets `local_llm_available: false`; the AI pipeline runs on
    Rules + FinBERT (+ Premium). No launch-time prompt. (The AI
    router already degrades gracefully when local reasoning is
    absent.)
  * **#2 → catalogue-only.** Both `install_local_model` and
    `set_local_llm_model` refuse any model id not in the supported
    catalogue. The custom-model Experimental escape hatch is deferred.

**Phase 5 — landed (2026-05-22).** Premium LLM provider picker +
compatibility test:
- `config/premium_llm_catalogue.yaml` — 5 supported providers
  (Anthropic, OpenAI, Gemini, Azure OpenAI, custom OpenAI-compatible).
  Two endpoint shapes cover all five: `anthropic_native` and
  `openai_compatible` (Gemini uses its OpenAI-compatible endpoint).
- `connectors/premium_llm.py` — `build_premium_llm_view` (per-provider
  configured state + active provider), `cert_premium_provider`
  (auth + structured-JSON + latency test against the live provider
  API; credentials read from env, never echoed), `set_premium_provider`
  (catalogue-only write to `ai.yaml`).
- Endpoints: `GET /connect/ai/premium/catalogue`,
  `POST /connect/ai/premium/test`, `POST /connect/ai/premium/activate`.
- `tests/test_d127_connect_hub_v2.py` extended to 61 tests. Full
  suite: 1602 passed, 3 skipped.

A custom OpenAI-compatible endpoint is accepted because the premium
LLM only advises — it never executes — but it must still pass the
compatibility test before activation.

**Phase 6 — landed (2026-05-22).** First-run onboarding wizard:
- `connectors/onboarding.py` — `build_onboarding_view` derives the
  four-step wizard (broker / feeds / AI / treasury) from the live
  Connect Hub snapshot, AI pipeline view, and machine probe. The
  broker + AI-core steps are required; feeds + treasury are optional.
  `can_launch` is true as soon as one broker is configured (a single
  paper broker runs the system); `ready_to_finish` is true when no
  required step is outstanding. Degrades safely on missing inputs.
- Persistence: only the "operator finished the wizard" flag is stored
  (`control_state` key `connect_hub.onboarding`), so the wizard does
  not reappear every launch.
- Endpoints: `GET /connect/onboarding`,
  `POST /connect/onboarding/complete`.
- `tests/test_d127_connect_hub_v2.py` extended to 68 tests. Full
  suite: 1609 passed, 3 skipped.

**Phase 7 — deferred to its own project (open decision #4, 2026-05-22).**
Connect Hub v2 ships at P6. Treasury stays **read-only** — fully usable
as a capital reference, no cash movement. P1–P6 are all read-only
inventory / config / advisory-gating; P7 is categorically different —
it designs and implements the contract for moving *real cash* between a
treasury account and brokers (approval workflow, daily caps, reserve
floor, beneficiary whitelist, audit). The design doc itself flagged it
as "separate, gated, after everything else is soak-tested". The
operator chose to defer P7 to a dedicated, carefully-scoped project
after the P1–P6 work has had a paper soak.

**Connect Hub v2 — final status: P1–P6 implemented (2026-05-22),
P7 deferred.** 68 tests in `tests/test_d127_connect_hub_v2.py`; full
repo 1609 passed, 3 skipped.

---

## D125.4 — Live deployment constraints cleared through execution/session routing (2026-05-22)

**Decision:** The low-deployment follow-up fixed the remaining live
execution constraints after D125.1-D125.3: clamped top-ups no longer get
vetoed by theme uniqueness, USD-quoted crypto can fall through from
Kraken to USDT venues when Kraken paper room is exhausted, execution can
reroute again when same-cycle venue reservations fill a crypto venue, and
IBKR/Alpaca equities/ETFs are tradeable during their real extended-hours
window.

**Why.** After the first fixes, the UI still showed ~20% at work. Live
logs showed the bottleneck had moved:
- `single_name_notional` correctly clamped existing FX/crypto top-ups,
  but `theme_uniqueness` then rejected the tiny same-theme residual
  top-up.
- Crypto `*-USD` signals hard-routed to Kraken even after Kraken's
  synthetic paper deploy room was zero.
- After Binance filled its first same-cycle crypto order, later crypto
  orders still routed to Binance and skipped because execution saw
  `room=50000 reserved=50000`.
- U.S. equities/ETFs were rejected as `market_closed` at 08:20 ET even
  though IBKR/Alpaca can transact the 04:00-20:00 ET extended-hours
  session in paper mode.

**Change.**
- `risk/engine.py`: a successful single-name clamp now stamps
  `sizing_topup_existing` / `risk_single_name_topup_clamped` metadata;
  `_check_theme_uniqueness` exempts clamped same-side existing-position
  top-ups so a size cap cannot be turned into a duplicate-theme veto.
- `execution/router.py`: canonical `BTC-USD`-style crypto still prefers
  Kraken while Kraken has deploy room, but when Kraken is exhausted it
  falls back to Binance spot first, then Bybit.
- `brokers/binance/adapter.py` and `brokers/bybit/adapter.py`: canonical
  `*-USD` crypto maps to the venue's `*USDT` book for no-native-paper
  execution.
- `execution/engine.py`: crypto venue-room reservations are exposed to
  execution-time fallback. If the chosen synthetic venue has zero
  effective room after same-cycle reservations, the order is rerouted to
  the next available crypto venue instead of skipped.
- `core/market_session.py` and `config/market_hours.yaml`: IBKR and
  Alpaca use broker-aware `by_asset_class_extended` tradeability for
  U.S. equities/ETFs/options. Session-exit management remains anchored
  to the regular close unless `MARKET_SESSION_EXTENDED=1` is globally
  set.

**Live verification.** After restart, the system moved from ~20% at work
to 60.1% on the first extended-hours cycle and 69.3% on the next; gross
exposure rose to ~$1.142M on ~$1.224M NAV (~93%). Remaining idle on the
UI's cash/margin-deployed gauge is now a candidate-breadth / cash-factor
issue: most current symbols are already at the 5%-NAV cap, while FX
counts at a 0.20 cash factor.

**Tests.** Focused verification:
`python -m pytest tests/test_d125_risk_caps.py tests/test_risk_engine.py tests/test_execution_engine.py tests/test_router_demand_bias.py tests/test_asset_class_routing.py tests/test_crypto_adapter_rejection_metadata.py tests/test_paper_wallet.py tests/test_instruments_canonical.py tests/test_instruments_availability.py tests/test_ibkr_universe_qualification.py tests/test_market_session.py tests/test_session_exit_policy.py -q`
-> 193 passed, 1 skipped.

---

## D125.1 — Single-name / per-day caps clamp instead of veto (2026-05-22)

**Decision:** The D125 `single_name_notional` and `intraday_symbol_adds`
risk gates now **clamp** an oversized order down to the cap rather than
**vetoing** it, and the per-day tracker is fed from actual fills.

**Why.** After the D126 clean-slate restart, deployment stalled at
~1.6% — 2 positions in hours. Investigation of the live log: 35 of the
last 40 risk rejections were `single_name_notional`. The allocator
(hunter mode, $1.22M NAV, mostly-cash book) sizes individual actions at
$63k–$321k; the 5%-of-NAV cap is $61k, so the gate rejected almost the
entire allocator output. A position-size *limit* was acting as a
deployment *blocker*.

**The flaw.** A per-name exposure cap should bound the position —
"buy at most 5%" — not refuse the trade. When the allocator wants $98k
of AAVE and the cap is $61k, the correct outcome is *buy $61k of AAVE*,
not buy nothing.

Second, smaller bug: `intraday_symbol_adds` recorded against its
per-UTC-day tracker at *risk approval*, not at *fill*. Approved signals
that never filled inflated the daily total (BCH-USD showed
`added_today=$110k` with zero BCH-USD fills) and wrongly blocked later
trades.

**Changes.**
- `RiskEngine._clamp_signal_to_notional` — resizes
  `signal.suggested_quantity` so its notional fits a target; never
  enlarges; updates `risk_notional_override` metadata if present.
- `_check_single_name_notional` and `_check_intraday_symbol_adds` —
  on an over-cap signal, clamp to the remaining room and APPROVE.
  Only REJECT when there is genuinely no room (existing position /
  day-total already at or over the cap) or no usable price to resize.
- Recording moved off the risk-approval path: `evaluate()` no longer
  touches the tracker; `ExecutionEngine._persist_fill_to_ledger` calls
  `record_open_signal_notional` on every confirmed non-reduce-only
  fill, so the per-day cap counts real fills.
- Reduce-only / arbitrage / options exemptions unchanged. The 5% and
  10% caps are unchanged — with clamping, 5% simply means the book
  naturally diversifies into ≥20 names to reach full deployment, which
  is the intended risk posture.

**Tests.** `tests/test_d125_risk_caps.py` updated — over-cap cases now
assert clamp-to-cap; hard reject retained only for the no-room case.
Full suite: 1611 passed, 3 skipped.

---

## D125.2 — Crypto paper-wallet room clamps instead of skips (2026-05-22)

**Decision:** The crypto synthetic paper-wallet venue-room guard in
`ExecutionEngine` now clamps an opening order down to the venue's
remaining deploy room instead of skipping the order outright.

**Why.** After D125.1, the primary risk veto was fixed and the live
system began filling IBKR FX at the 5%-of-NAV cap. The remaining crypto
execution gap was the same mechanism one layer later: post-risk crypto
orders were clamped to ~5% of total NAV (~$61k), but each no-native-paper
crypto venue has its own synthetic wallet (`$50k` default seed, less
open gross exposure). A `$61k` BTC/XRP/ETH order routed to Kraken with
only ~$30k room became `crypto_venue_capital_exhausted` and bought
nothing.

**Change.** In paper mode for Kraken/Binance/Bybit, when an opening
crypto order's notional exceeds `system.paper_wallet.venue_deploy_room`,
execution now resizes `order.quantity` and `signal.suggested_quantity`
to exactly fit the room, stamps `crypto_venue_room_clamped` and
`crypto_venue_room` metadata, and continues to the normal simulated fill.
It still skips when room is zero or no usable price exists. Reduce-only
and close intents remain exempt.

Because `venue_deploy_room` is heartbeat/snapshot-backed, execution also
keeps an in-process per-venue reservation for the current room snapshot.
That prevents multiple orders in the same loop from each seeing the same
stale `$30k` room and all filling against it before the wallet heartbeat
has written the updated gross exposure.

**Tests.** Added execution coverage for room-clamp and true zero-room
skip. Focused verification:
`python -m pytest tests/test_d125_risk_caps.py tests/test_paper_wallet.py tests/test_execution_engine.py -q`
-> 67 passed.

---

## D125.3 — IBKR crypto registry guard (2026-05-22)

**Decision:** IBKR instrument-registry translation now whitelists only
known PAXOS crypto bases and emits the bare IBKR crypto base symbol
(`BTC`, `SOL`, etc.) instead of letting canonical crypto pairs leak into
stock qualification.

**Why.** The live low-deployment audit showed repeated IBKR API errors
for contracts such as `Stock(symbol='BTC-USD', exchange='SMART',
currency='USD')`, plus account-summary request pressure. Those were not
valid equity contracts; they were canonical crypto symbols being treated
as IBKR stock candidates by downstream qualification paths.

**Change.** `instruments.canonical.canonical_to_broker()` now returns an
IBKR broker symbol only for the known PAXOS whitelist, and returns
`None` for unsupported crypto such as `AAVE-USD`. `IBKRAdapter` now
classifies canonical supported crypto (`BTC-USD`, `SOL-USD`, etc.) as
`Crypto(..., "PAXOS", "USD")`; unsupported canonical crypto fails
locally with `unsupported IBKR PAXOS crypto symbol` instead of sending a
bad stock qualification request to TWS. Snapshot price reads and stream
subscriptions also skip unsupported canonical crypto locally, so the
market-data path cannot recreate the same `Stock(symbol='*-USD')` storm.

**Tests.** Added canonical translation, availability, and IBKR adapter
qualification coverage. Focused verification:
`python -m pytest tests/test_d125_risk_caps.py tests/test_paper_wallet.py tests/test_execution_engine.py tests/test_instruments_canonical.py tests/test_instruments_availability.py tests/test_ibkr_universe_qualification.py -q`
-> 96 passed.

---

## D126.1 — NAV baseline re-anchored to real broker totals (2026-05-22)

**Decision:** Re-anchor `control_state::paper.nav_seed` to the live sum
of broker paper-account balances, and correct the misleading "NAV
resets to paper.nav_seed" claim in `scripts/reset_trading_data.py`.

**The gap.** D126's data reset wiped mytbot's DB and reset
`paper.nav_seed` to $1,072,898, and the reset script reported "NAV
resets to paper.nav_seed". That was wrong. NAV is computed **live** as
the sum of the connected brokers' paper-account balances — IBKR's TWS
paper account (`NetLiquidation`), Alpaca's paper account, and the
crypto paper wallets (`system/paper_wallet.py`). The wipe cleared
mytbot's *ledger* but cannot reach those broker-side balances. After
the post-reset restart, once all five brokers connected, NAV settled
at the real total **$1,224,361** — IBKR alone ≈ $1,016,548 (still
carrying its pre-D126 paper history). `paper.nav_seed` is only a
pre-broker-connect fallback; it never drove the displayed NAV. The
apparent "+$151k jump" was simply the stale seed vs. the real
broker total surfacing as IBKR (which connects late) came online.

**Resolution.** Operator chose to re-anchor the baseline to reality
rather than reset the broker paper accounts at source:
- `paper.nav_seed` set to the live broker total ($1,224,361.13), with
  a `reanchored_at` stamp.
- the stale `nav.opening_snapshot` (recorded before IBKR connected, 4
  brokers, $207,824) deleted so it re-records a complete all-broker
  snapshot.
- `scripts/reset_trading_data.py` docstring + output corrected with a
  NAV CAVEAT: the wipe clears the DB ledger only; real NAV is
  broker-derived and the seed must be re-anchored after brokers
  connect (or the broker paper accounts reset at their source —
  IBKR via TWS "Reset Paper Trading Account", Alpaca via its
  dashboard, crypto via `PAPER_WALLET_*_USD`).

The DB ledger from D126 remains genuinely clean; only the NAV baseline
needed reconciling with the broker accounts the wipe could not touch.

---

## D126 — Fills ledger + phantom-oversell root-cause fix + data reset (2026-05-21)

**Decision:** Replace the corrupted, un-attributable trading history
with a clean append-only `fills` ledger, fix the root cause of the
phantom oversells, and wipe the corrupted tables for a 100%-accurate
restart.

**The corruption.** The `orders` table could not be reconciled:
79,910 BALL shares sold against only 10,586 ever bought; AAPL oversold
by ~10%; $148M of gross turnover on a $1.18M book in 30 days. Per-symbol
and per-broker P&L were unrecoverable.

**Root cause.** `PositionLog` is append-only with "latest row per
`(broker, symbol)` by `max(timestamp)`" as the current position. Five+
independent coroutines (main loop, stop-loss, profit-harvest,
intraday-derisk, aggregate-derisk, mark-refresh sweep, reconciler) each
do a **non-atomic read-modify-write**: load latest → mutate → append a
new row. When coroutine B loaded the snapshot *before* coroutine A's
sell persisted, B's later-timestamped row resurrected the pre-sell
quantity — erasing A's sell. The next derisk tick saw the full position
again and sold it a second time. No lock, no version, and critically no
"can't sell more than you hold" guard. D125 #2 fixed the *concurrent*
(within-30s) case; this defect operates over hours.

**The fix — fills ledger as the race-free authority.**

- New `fills` table (`storage/models.py::FillLog`, migration
  `d126f1a2b3c4_fills_ledger.py`): append-only, one row per confirmed
  fill. A position's quantity for `(broker, symbol)` is exactly
  `SUM(signed_quantity)` over its fills — pure appends never conflict,
  so the resurrection race cannot occur. Columns cover the full
  analytics surface: realised P&L (weighted-average cost), cost basis,
  position-qty-after, holding period, strategy, signal id +
  confidence, mode, notional, asset class, side, order type,
  reduce-only, is-paper, run-session id, derisk source.
- `storage/fills_ledger.py`: `record_fill` (the single write-path,
  serialized under one `asyncio.Lock` — read-prior-state /
  compute-WAC / append is atomic), `available_quantity` and
  `position_state` (race-free SUM queries).
- **Oversell guard** (`ExecutionEngine._clamp_reduce_only_to_holdings`,
  called in `execute()` before order build): every reduce-only /
  closing order is clamped to the fills-ledger holding. It can never
  sell more than is held; if the ledger shows the position flat, the
  order is skipped entirely. When the ledger has no rows for a symbol
  yet (only possible pre-reset) it is non-authoritative and the guard
  no-ops. This sidesteps the snapshot race rather than trying to
  serialize every PositionLog writer — the money-critical decision no
  longer trusts the racy snapshot at all.
- Write-path: `ExecutionEngine._persist_result` → `_persist_fill_to_ledger`
  appends a `fills` row after every confirmed fill (the single
  chokepoint all execution paths already pass through). Arbitrage
  fills are skipped (bundle-level P&L doesn't fit the single-symbol
  WAC model).

**Data reset.** `scripts/reset_trading_data.py` (dry-run by default,
`--execute` to perform, refuses `APP_ENV=live`). TRUNCATEs
`orders, positions, fills, daily_pnl, signals, risk_decisions,
strategy_candidate_log, thesis_log, anomaly_log`; deletes all
`control_state` keys except the operator/config whitelist
(`paper.nav_seed`, `system.capital_allocation`, `strategy.enabled.*`,
`auto_training.last_run_at`); removes `data/runtime/risk_state.json`.
NAV resets to `paper.nav_seed` ($1,072,898.74 — the original paper
capital; the phantom-inflated +10.5% is discarded as untrustworthy).
Keeps feature snapshots, price history, instrument registry, models,
news, macro, parameter log, config.

**WAC accounting.** `realised_pnl` is GROSS trading P&L; `fee` is a
separate column. Net P&L for any slice = `SUM(realised_pnl) - SUM(fee)`.
Realised P&L and holding period are populated only on
position-decreasing fills.

**Tests.** `tests/test_d126_fills_ledger.py` — 17 tests: WAC math
(open / add / partial close / full close / long→short flip / short
cover), DB-backed ledger (accumulation, realised P&L, bad-input
rejection, race-free SUM), and the oversell guard (empty-ledger
allow, within-holdings pass, over-holdings clamp, flat-skip,
wrong-direction skip). All pass.

**Deploy sequence (operator).** 1) stop the trading system; 2) the
D126 code is already on disk and the migration has run; 3)
`python scripts/reset_trading_data.py --execute`; 4) restart
`python run.py`. From the first fill onward the `fills` ledger is the
complete, accurate record — by-broker / by-strategy / by-symbol
analytics all reconcile against NAV.

**Residual / follow-up.** The `positions` snapshot table still has the
multi-writer race — but it is now display-only; the oversell guard
makes it incapable of causing money loss. A future change could
rebuild `positions` from the fills ledger each reconcile cycle to
eliminate display drift too. Out of scope for D126.

**Status:** Code + migration + tests landed; `fills` table created.
Data wipe is pending operator go (system must be stopped first).

---

## D125 — BF-B concentration fix bundle (2026-05-21)

**Decision:** Five hardening changes triggered by the 2026-05-21 BF-B
audit, where a single common equity reached 28.5% of NAV via 38
consecutive volume_flow buys and the resulting trim was double-sold by
two independent derisk loops.

**Audit summary.**

- BF-B grew from $0 to $341,930 over 35 hours (05-20 02:54 → 05-21 03:26
  UTC) through 38 buy signals, all volume_flow with conf 0.71–0.79.
- Per-action sizing was bounded (~$39k pre-mode), but hunter mode
  amplified each action up to 17× (cap $640k) and `topup=True`
  exempted them from any "we already own a lot of this" check.
- All legacy single-stock caps were inert by design
  (`enforce_static_exposure_caps: false` is the documented default).
- The intraday-derisk monitor fired every ~10 minutes pre-NYSE-open
  trying to trim BF-B — 8 silent rejections between 13:00–13:30 UTC
  because the market-session gate inside `execute()` bounced them.
- At 13:39:28 UTC (post-open) intraday-derisk finally submitted
  `sell qty=6598.5`. **4.5 seconds later** aggregate-derisk submitted
  an identical `sell qty=6598.5`. Net result: position closed
  completely (13,197 shares sold against a 6,598.5-share intent),
  then re-bought 2,242 shares by a fresh mean_reversion signal four
  minutes later.
- The "MARKET CLOSED" / "EXECUTING" / "PAPER FILL" / "EXEC SKIP" /
  "SIZING GUARD REJECT" lines from `execution.engine` were
  invisible across 10MB of `logs/mytbot.log` over many hours — the
  module uses `logging.getLogger(__name__)` and the loguru file sink
  had no bridge to receive stdlib records.

**Changes.**

1. **Single-name notional cap** (`risk/engine.py::_check_single_name_notional`,
   `config/risk_limits.yaml::single_name_notional`). Hard ceiling at
   5% of NAV per symbol on any new open, evaluated against
   `existing_position_notional + proposed_signal_notional`. Enforced
   UNCONDITIONALLY (does not consult `enforce_static_exposure_caps`).
   Reduce-only / arbitrage / options exempt.
2. **Cross-loop derisk dedup**
   (`Orchestrator._derisk_inflight_ts` + `_derisk_inflight_window_sec`).
   Single shared registry keyed by `(broker, symbol)`. Both
   intraday-derisk and aggregate-derisk loops check the lock before
   submitting a close/trim and set it on success. Default 30s window
   covers a paper-fill round trip and a normal IBKR ack; tunable via
   env `DERISK_INFLIGHT_WINDOW_SEC`.
3. **stdlib→loguru bridge** (`run.py::_LoguruInterceptHandler` +
   `logging.basicConfig(handlers=[...], force=True)`). All stdlib
   `logging` records (execution.engine, execution.router, execution.planner,
   execution.wave9_runtime, ai.fusion, brokers.permissions, …) now
   forward into the loguru file sink with correct module / function /
   line attribution. The 24+ silent execution-path log statements
   that hid the BF-B reject pattern are visible again.
4. **Closed-session derisk deferral**
   (`Orchestrator._symbol_is_tradeable_now` invoked inside both derisk
   tick handlers). Per-symbol filter that drops derisk actions whose
   venue session is closed at the time of the tick, deferring with an
   INFO log instead of submitting a guaranteed-to-bounce order.
   Crypto / unrecognised classes default to tradeable so a real close
   is never falsely blocked. Gate failsafes return True on exception.
5. **Per-UTC-day cumulative-add cap**
   (`risk/engine.py::_check_intraday_symbol_adds` +
   `RiskEngine.record_open_signal_notional` +
   `config/risk_limits.yaml::intraday_symbol_adds`). Cumulative cap at
   10% of NAV per symbol per UTC day. Bookkeeping is optimistic —
   incremented at risk APPROVAL not at fill (overestimating cumulative
   adds is the safe direction for a defensive cap). Tracker resets at
   UTC midnight via `_roll_intraday_adds_day_if_needed`. Reduce-only /
   arbitrage / options exempt.

**Why each cap is unconditional.** The "adaptive sizing replaces fixed
caps" philosophy embedded in `enforce_static_exposure_caps: false` has
no portfolio-level awareness — the per-signal sizer can't tell that
this is the 38th buy on the same ticker. D125 reintroduces the smallest
useful set of hard rails as separate, opt-out config blocks (rather
than reviving the legacy `max_single_stock_pct` family) so a future
operator who wants pure adaptive sizing can set `enabled: false` on
each without surprising the rest of the engine.

**What did NOT change.** Adaptive sizing, mode amplification, hunter
mode's `max_notional_fraction_per_action`, accumulator integration,
meta-labeler v0.2.0, D122 dynamic thresholds, intraday-derisk tier
table, aggregate-derisk close-budget logic. The derisk loops still
identify the right symbols to trim — they just don't oversell, don't
shout into closed venues, and can't paper-over a 28%-of-NAV
concentration the engine should have refused upstream.

**Tests.** `tests/test_d125_risk_caps.py` (15) +
`tests/test_d125_derisk_dedup.py` (7) = 22 new tests, all passing.
Three pre-existing tests updated to disable the new D125 caps in
their fixtures (`tests/test_fx_cluster_exposure.py`,
`tests/test_equity_index_cluster.py`, `tests/test_dynamic_scaling_stress.py`,
`tests/test_risk_engine.py`) so they continue to exercise their
focused subject (cluster logic, dynamic scaling, legacy-opt-in
semantics) without tripping the new unconditional rails. Full repo
regression: **1524 passed, 3 skipped** (one unrelated pre-existing
failure: `test_instruments_sources_wikipedia.py` missing `lxml`
optional dep).

**Status:** Implemented. Restart `python run.py` to activate. After
restart, expect:
- `RISK single_name_notional REJECT | <SYMBOL> | ...` warnings if any
  strategy tries to compound past 5% of NAV on one ticker — these are
  the system saving you from the next BF-B.
- `orchestrator | intraday-derisk deferred | ... | venue session closed`
  INFO lines pre-NYSE-open instead of silent execute() bounces.
- `EXECUTING / PAPER FILL / EXEC SKIP / MARKET CLOSED / SIZING GUARD
  REJECT` lines from execution.engine landing in `logs/mytbot.log`
  for the first time.

---

## D124 — Auto-training embedded in orchestrator (2026-05-21)

**Decision:** Move daily auto-training from a standalone Windows
scheduled task into the orchestrator as a background coroutine.
Delete the `scripts/install_auto_training_task.ps1` installer and
unregister the existing `mytbot-auto-training` Task Scheduler entry.

**Why.** D123 installed the Windows task that
`config/auto_training.yaml` had been declaring for weeks without
anyone noticing it was never actually registered — that silent drift
is exactly the failure mode the one-button principle exists to
prevent. The separate scheduled task also:
- broke "`python run.py` starts everything" (CLAUDE.md rule),
- was invisible to `/system/status` and the UI,
- required re-installation on every new machine
  (`docs/NEW_MACHINE_SETUP.md`),
- and gave no real isolation benefit since the same `.venv` + `.env`
  was loaded anyway.

The original isolation arguments (crash containment, missed-run
recovery, ability to run while trading is OFF) are addressed by
running training as a subprocess from the orchestrator coroutine:
`asyncio.create_subprocess_exec(sys.executable,
"scripts/auto_train_models.py")`. A training crash dies in the
subprocess and the orchestrator logs the non-zero exit code; the
trading loop is unaffected.

**What changed.**

- `system/orchestrator.py`: new `_start_auto_training_loop` /
  `_auto_training_loop` / `_auto_training_tick` /
  `_run_auto_training_job` / `_resolve_auto_training_config` /
  `_persist_auto_training_last_run` /
  `_load_persisted_auto_training_last_run`. Wakes once per minute,
  checks `config/auto_training.yaml::auto_training.{enabled,
  schedule.start_time_local, timezone}`, fires once per local day
  after the configured time. Last-run timestamp persists to
  `ControlState` under `auto_training.last_run_at` (new key
  `Orchestrator.AUTO_TRAINING_STATE_KEY`) so a restart cannot
  trigger duplicate runs.
- `config/auto_training.yaml`: `schedule.windows_task_name` removed
  (no longer relevant); comment block updated to describe the
  embedded scheduler.
- `scripts/install_auto_training_task.ps1`: deleted.
- Windows Task Scheduler `mytbot-auto-training`: unregistered.
- `tests/test_auto_training_scheduler.py`: 8 unit tests covering
  enabled-flag gating, before/after scheduled time, prior-run-today
  vs prior-run-yesterday, subprocess-already-running guard, and YAML
  resolver.

**Operational notes.**

- The trading loop starts the scheduler in `start()` right after
  `_start_zero_alloc_flatten_watchdog()` and cancels it in `stop()`.
- The scheduler stays alive across `/system/stop` → `/system/start`
  cycles only if the trading loop itself stays up; this matches
  every other background task. If the operator wants training to
  run even while trading is OFF, leave `python run.py` alive (the
  scheduler runs inside the orchestrator process, not the trading
  loop, so it survives ON/OFF toggles of the trading loop).
- New-machine setup no longer requires installing a scheduled
  task; updating `docs/NEW_MACHINE_SETUP.md` to drop that step is
  a follow-up.

**Status:** Implemented. Restart `python run.py` to activate.

---

## D123 — Meta-labeler v0.2.0: dedup fix + 30-day retrain + auto-training task (2026-05-21)

**Decision:** Retrain `mytbot_meta_labeler` as v0.2.0 with two construction
fixes and install the daily auto-training scheduled task.

**Why.** A 12-hour live sample (3,672 candidates) showed v0.1.0 placed 67%
of candidates in calibration bin 0.25–0.30 (3.2% historical hit rate) and
only 1.6% above 0.40 — pinning live capital deployment at ~45% despite
the operator's 100% slider. Audit traced this to two defects, neither
of which is a D122 dynamic-threshold tuning issue:

1. **Feature duplication.** `scripts/build_meta_label_dataset.py` wrote
   both `news_score` and `accumulator_score` into the v0.1.0 CSV. At
   build time `sig.news_score` was `None`, the script fell back to
   `md["ai_news_score"]`, and that field was being populated from the
   accumulator's own AI-news rollup — so the two columns were
   byte-identical (`mean=0.3167, std=0.2176` for both). The logistic
   regression's effective weight on that signal was doubled.
2. **Stale, narrow window.** v0.1.0 was built 2026-04-27 from 3,679
   rows dominated by `mean_reversion`. Live distribution since has
   broadened to momentum/volume/volatility/event/regime/pairs, and
   `accumulator_score` has drifted from training mean +0.317 to live
   mean ~−0.014 — a 1.5σ shift that alone moves logreg predictions
   from ~0.42 to ~0.30.

**What changed.**

- `scripts/build_meta_label_dataset.py`: `news_score` removed from
  `FEATURE_COLUMNS`. With `sig.news_score` only (no `md` fallback),
  the live correlation between `news_score` and `accumulator_score`
  is still 0.967 — independence cannot be guaranteed today, so the
  column is dropped entirely. Accumulator carries the news
  information. The column can return in v0.3.0 when an independent
  point-in-time AI-news source is wired in.
- Fresh dataset: `data/research/meta_label/20260521_meta_label_v0_2_0`
  — 13,215 leakage-safe rows from 22,622 signal-log rows over the
  prior 30 days × 1,697,248 feature-snapshot rows.
- Artefact: `artifacts/models/meta_label/mytbot_meta_labeler-0.2.0.pkl`
  (logreg + Platt, 5-fold purged CV, embargo 10 bars). New
  `feature_contract_hash`:
  `e1d439adc21b8a120b22186b5f79a7261389e4155ad15254eb42df0ccbb8d9d6`.
- `config/model_registry.yaml`: v0.2.0 registered at
  `approval_status: paper`, `calibration_table` populated from the
  held-out 30% temporal-split OOS bins with `n≥100`. D122 reads
  this table directly.
- `config/meta_labeler.yaml`: `model_version: 0.2.0`,
  `artifact_path` updated. Rollback is single-key.
- Windows scheduled task `mytbot-auto-training` registered
  (daily 03:20 local, runs `scripts/auto_train_models.py` via venv
  python). Verified `State=Ready`; `config/auto_training.yaml`
  was declaring the cadence but the task itself had never been
  installed prior to this commit.
- Validation report:
  `reports/models/mytbot_meta_labeler/0.2.0/validation.md`.

**OOS results (temporal 70/30 split).** Brier 0.221 ≤ 0.25 (pass).
High-confidence lift: v0.2.0 best populated bin (predicted 0.758, n=93)
delivers observed 0.763 — vs v0.1.0 best populated bin (predicted
0.618, n=93) observed 0.419, on the same test slice. D122
simulation: at `target_win_rate=0.42`, v0.2.0 deploys ~95% of
candidates against a calibrated bin (predicted 0.228, observed 0.456,
n=136), where v0.1.0's equivalent threshold (0.330) lands on a noise
spike in a bin with no reliable mid-band signal.

**Known caveats (paper-soak monitoring).**

- Train→test base-rate drift (0.41 → 0.20) in the last 9 days drives
  high OOS ECE (0.259). The 30-day window straddles a regime shift.
- Mid-band calibration (predicted 0.43–0.62) over-predicts. D122
  `target_floor=0.20` keeps the operational gate below this band;
  do not raise the floor above 0.45 without a fresh calibration.
- Trainer convergence warnings (lbfgs hit `max_iter=400` on all 5
  CV folds + final fit) — features are not standardised in
  `models/meta_label/train.py::_make_classifier`. Out of scope for
  v0.2.0; flag for a future trainer-side StandardScaler step.

**Non-changes.** D122 dynamic threshold resolver and its config are
untouched. `target_floor` was not lowered (per non-goal: do not mask
calibration evidence). v0.1.0's row remains in the registry for
rollback; promotion to micro_live/live blocked until ≥14 days of paper
soak per `docs/MODEL_GOVERNANCE.md`.

**Status:** Implemented and live on the trading loop after restart.
Verification gate during paper soak: live candidate probability
histogram should center near ~0.43 (training mean) rather than v0.1.0's
~0.29; deployment % should organically climb toward 70–85% in neutral
regime at 100% slider. If deployment remains <60% in neutral regime,
escalate as a data-quality investigation (drift, not threshold-tuning).

---

## D118 — Self-tuning priority pre-filter + 6-stage universe funnel (2026-05-19)

The funnel between "every unique normalized symbol from connected brokers
+ the instrument registry" and "the small set we actually score with
yfinance" was a problem on three axes:

1. **It was opaque.** The operator-visible funnel jumped from
   `broker_listings` → `eligible` → `watching` → `active_reps` with no
   visibility into how we narrowed ~16k unique symbols down to the
   ~400 we sent to yfinance.
2. **It had hidden randomness.** `_stratified_sample_candidates` did a
   deterministic but effectively stratified-random pull weighted by
   broker tier — there was no causal rule the user could read.
3. **It still relied on hard-coded knobs.** The scoring budget (320),
   the eligible-vs-pinned cutoffs, and any "weights" that would be
   added later (liquidity, freshness, asset-class balance) were all
   tunable numbers in YAML. The operator demanded none of this be
   manually set.

**Decision.** Replace stratified-random candidate selection with a
deterministic, self-tuning rule that scores **every** unique
normalized symbol fast (microseconds per symbol; no I/O), picks the
top-N by score, and self-adjusts both the weights of the score
components and N itself based on observed outcomes. There are **no
operator-tunable numbers** for either the weights or the budget — only
master kill switches and (fixed, not-tunable) safety bounds in code.

**Four-stage funnel** (`universe/snapshot_service.py::_build_d118_funnel`):

1. `unique_normalized` — every unique broker-listed symbol + every
   instrument registry symbol after canonicalisation/deduplication.
   The raw broker-listing count is shown as a debug tooltip but is
   **not** a stage (the user explicitly rejected the 31k row).
2. `scored` — top-N priority pick **and** yfinance liquidity scoring in
   one pipeline pass. The self-tuning budget N and any timeout gap
   (`budget_attempted` vs count) are exposed on `scored.meta`.
3. `watching` — `core + scan` tiers from `universe_tiers.json`.
   Temporary anomaly promotions (`promoted_now`) are metadata on this
   stage, not a separate funnel step — they overlap scan/light and are
   not a filter between watching and active reps.
4. `active_reps` — non-redundant correlation representatives from
   universe intelligence clustering.

**Priority rule** (`data/universe_prefilter.py::compute_priority_scores`):

A symbol's priority is a weighted sum of six component subscores in
`[0, 1]`:
- `liquidity_prior` — registry liquidity bucket, fallback heuristics
  for crypto / FX / ETFs.
- `anchor_pin` — 1.0 for `UniverseManager.INITIAL_UNIVERSE` plus the
  IBKR curated seed, 0.0 otherwise. Anchors are pinned post-rank.
- `freshness_bonus` — decays from 1.0 (never scored) to ~0.0 (scored
  in the last 60s); promotes coverage of rarely-touched symbols.
- `registry_availability` — score is 1.0 when at least one broker has
  the symbol available, 0.0 when registry says unknown.
- `asset_class_balance` — boost when the current `unique_normalized`
  set is light in this asset class.
- `region_balance` — same idea for region.

**Self-tuning weights**
(`data/universe_weight_learner.py::WeightLearner`):

Online logistic regression with AdaGrad and EWMA decay. After every
pipeline cycle, each picked symbol is labelled by "did it actually
enter `watching` this cycle?". Weights are updated to maximise that
labelled likelihood, clamped to `[0.05, 0.50]` per component
(safety bounds, not tunable), then re-normalised to sum to 1.0. State
persisted atomically to `data/runtime/universe_weights.json`.

**Self-tuning budget**
(`data/universe_budget_controller.py::BudgetController`):

Two control laws run on every cycle and the smaller of the two values
wins:
- **AIMD throughput control** — measured cycle wall-time vs the
  scoring interval. If we ran in less than the configured throughput
  share we add `+25` (additive increase); if we overran we multiply by
  `0.75` (multiplicative decrease).
- **Utility saturation detection** — track `max_watching_rank` (the
  deepest priority-ranked index that made it into `watching`). If for
  several cycles all watching members sat in the top `0.6 * budget`
  ranks, the marginal symbol scored is not yielding new watching
  members; we shrink toward that observed cap.
A hard `[budget_floor, budget_ceiling]` (200–800 by default; safety
bounds, not tunable knobs) clamps the result. The currently *binding*
constraint (`aimd_grow` / `aimd_shrink` / `utility_saturation` /
`floor` / `ceiling` / `stable`) is exposed in the UI so the operator
can see *why* the budget moved. State persisted atomically to
`data/runtime/universe_budget.json`.

**Tier-transition stream**
(`data/universe_transitions.py::TransitionBuffer`):

Every cycle, the new vs previous tier maps are diffed and the changes
are appended to a ring buffer (default 500 events) with
`(ts, symbol, from_tier, to_tier, reason, score_delta)`. Reasons
include `promoted_to_watching`, `demoted_to_light`,
`promoted_to_active_reps`, `entered_unique_normalized`,
`removed_from_universe`. State persisted atomically to
`data/runtime/universe_transitions.json`.

**Per-symbol score-age telemetry**
(`data/universe_score_ages.py::ScoreAges`):

For every unique normalized symbol we keep
`(last_scored_at, last_score, score_count, first_seen_at)` with
atomic JSON persistence and a hard cap on tracked size with a
deterministic LRU eviction (unscored first, then oldest scored).
`/intelligence/universe` returns the per-symbol last-score timestamp,
and the Instruments tab renders a coloured age stripe.

**Wiring.** `_pipeline_runner` in `system/orchestrator.py` now:
1. Loads `ScoreAges`, `WeightLearner`, `BudgetController`, and the
   previous tier snapshot.
2. Computes `priority_scores` over the full `unique_normalized` set.
3. Calls `BudgetController.compute_next_budget()` for `N`.
4. Calls `UniverseBuilder.build_tiered_universe(...,
   priority_scores=..., target_budget=N, anchors=...,
   telemetry=BuildTelemetry())`.
5. After the build: updates `ScoreAges` from the telemetry, updates
   `WeightLearner` from
   `build_training_rows(picks_breakdowns, watching_now)`, observes a
   `CycleObservation` into `BudgetController`, diffs old vs new tiers
   and records transitions. All state is persisted atomically.

**Config surface** (`config/data_pipeline.yaml::dynamic_universe.ranking.priority_score`):
```
priority_score:
  enabled: true
  weight_learning_enabled: true
  budget_self_tune_enabled: true
  state_dir: data/runtime
```
Only master kill switches and a state directory. Disabling any switch
falls back to uniform weights or a fixed budget; the entire `enabled:
false` path is the legacy `_stratified_sample_candidates` behaviour
unchanged.

**Backward compatibility.** When the priority rule is disabled (or
when no scores are passed in), the builder still runs
`_stratified_sample_candidates` exactly as before. Anchors from
`UniverseManager.INITIAL_UNIVERSE` continue to be pinned. The risk
engine, order routing, and broker availability are untouched: D118 is
a *discovery-layer* control only.

**Status:** Implemented (`data/universe_score_ages.py`,
`data/universe_prefilter.py`, `data/universe_weight_learner.py`,
`data/universe_budget_controller.py`, `data/universe_transitions.py`,
`data/universe_builder.py`, `system/orchestrator.py`,
`universe/snapshot_service.py`, `ui/src/app/lib/api.ts`,
`ui/src/app/redesign/universe/UniverseScreen.tsx`,
`config/data_pipeline.yaml`). Tests:
`tests/test_universe_score_ages.py`,
`tests/test_universe_prefilter.py`,
`tests/test_universe_weight_learner.py`,
`tests/test_universe_budget_controller.py`,
`tests/test_universe_transitions.py`,
`tests/test_universe_builder_priority_selection.py`,
`tests/test_universe_snapshot_d118.py`.

## D117 — Adaptive universe-tier sizing (regime + signal pressure + cluster count + anti-churn) (2026-05-19)

The pre-D117 universe funnel used three hard-coded caps in
`config/data_pipeline.yaml::dynamic_universe` (`max_symbols=300`,
`ranking.core_max=50`, `ranking.scan_max=250`,
`ranking.max_candidates_to_score=400`). These were identical in every
market state — risk-on, risk-off, low-vol, crash — and identical
regardless of how many high-conviction candidates the allocator was
actually finding. That had two costs:

1. We spent the same yfinance/feature-ingest budget in calm and noisy
   periods, even when the allocator had no place to deploy it.
2. We had no automatic widening when more idiosyncratic correlation
   clusters appeared (more independent bets available), and no
   focusing when the active cluster count collapsed.

**Decision.** Add `universe/adaptive_caps.py` — a pure decision module
that takes
`(regime_state, signal_pressure, active_cluster_count, config_bounds)`
and returns the resolved caps `(candidates, watching, core, scan)`,
clamped to YAML-declared min/max bounds. Each axis contributes a
multiplier:

- **Regime axis** (`config/data_pipeline.yaml::dynamic_universe.adaptive.regime`):
  `risk_on=1.25`, `trend_up=1.15`, `volatile=1.30` (more places to
  fish), `mixed=1.00`, `range=0.90`, `risk_off=0.80`, `crash=0.65`,
  `insufficient_data=1.00`.
- **Signal-pressure axis** (`adaptive.signal_pressure`): the recent
  `dashboard_feed.batch_candidate_count` is read from the persisted
  `dashboard.snapshot`. When ≥ `high_threshold` (default 8) the scan
  tier widens by `1.20`; when ≤ `low_threshold` (default 2) it narrows
  to `0.80`.
- **Cluster-aware floor** (`adaptive.cluster_aware`): when an honest
  correlation-cluster count is available
  (`data/runtime/universe_intelligence.json::clusters`), watching is
  lifted to at least `max(watching_min_floor, watching_min_factor *
  active_reps)`, then clamped to the bounds. Default: 150 floor, 3.0
  factor — so 88 active clusters gets `max(150, 264)=264` watching.
- **Anti-churn hysteresis** (`adaptive.churn`): a symbol that was in
  the previous build's watchlist but missing this build is *graced*
  into the scan tier for up to `min_consecutive_drops` (default 3)
  consecutive rebuilds before it actually drops to light. Prevents
  single liquidity blips from churning feature ingest.

Bounds enforce a hard ceiling and floor per tier
(`candidates 200-800`, `watching 150-600`, `core 25-100`,
`scan 75-500` by default). Invariants are also enforced post-resolve:
`core <= watching` and `scan + core <= candidates`.

**Wiring.**

- `_pipeline_runner` in `system/orchestrator.py`: each tick now reads
  the persisted dashboard snapshot, builds an `AdaptiveCapsContext`
  via `universe.adaptive_context.build_adaptive_caps_context`,
  resolves caps via `compute_adaptive_caps`, applies them through
  `UniverseBuilder.update_caps(...)`, then calls the existing
  `build_tiered_universe`. After the build, `apply_churn_hysteresis`
  re-includes graced symbols and re-persists the tier file. Resolved
  caps + miss counter + grace history land in
  `data/runtime/universe_adaptive_state.json`.
- `universe/snapshot_service.py::_pipeline_caps()` overlays the
  resolved caps so `/intelligence/universe` shows the active values
  the builder actually used, not the static YAML anchor.
- `/intelligence/universe` now returns an `adaptive` block with the
  resolved caps, base anchor, composite multiplier, per-axis
  multipliers, cluster-floor flag, and reasons. The Universe
  dashboard renders an `adaptive Nx ↑ widen / ↓ focus / · neutral`
  badge with a tooltip listing reasons.

**Backward compatibility.** Set `dynamic_universe.adaptive.enabled:
false` and the resolved caps equal the base caps unconditionally,
matching pre-D117 behaviour exactly. Missing or malformed YAML, missing
dashboard snapshot, missing intelligence file → neutral context →
multiplier 1.0. The risk engine, order routing, and broker availability
are untouched: D117 is a *discovery-layer* control only.

**Status:** Implemented (`universe/adaptive_caps.py`,
`universe/adaptive_context.py`, `universe/adaptive_state.py`,
`config/data_pipeline.yaml::dynamic_universe.adaptive`,
`data/universe_builder.py::UniverseBuilder.update_caps()`,
`system/orchestrator.py::_pipeline_runner`,
`universe/snapshot_service.py`, `ui/src/app/redesign/universe/UniverseScreen.tsx`,
`ui/src/app/lib/api.ts`). Tests:
`tests/test_universe_adaptive_caps.py` — 26 tests covering disabled
fallback, each regime label, signal-pressure axis, bounds clamping,
cluster-aware floor (lift, max-clamp, no-shrink, disabled),
hysteresis grace (extend, drop-after-N, reset, no-previous-state),
config loader (good/missing/malformed YAML), runtime state round-trip,
and post-resolve invariants.

## D116 — Instrument Registry + Cross-Broker Availability Resolver (2026-05-19)

The hand-maintained per-broker symbol lists (especially IBKR's 61-line
curated YAML seed) were the binding constraint on universe coverage and
were not adapting when new brokers were added. The fix is a self-updating
master instrument table sourced from public maintained references, with
per-broker availability tracked separately.

Decision:

- A new instrument registry layer is introduced as a strictly read/observe
  consumer; it does not participate in any signal, risk, or order path.
  Schema (Postgres):
    * `instrument_registry` — canonical master (yfinance-style symbol PK,
      asset class, region, exchange, currency, sector/industry, ISIN,
      FIGI, first/last/refreshed seen timestamps, retired_at, metadata)
    * `instrument_source_membership` — one row per `(canonical_symbol,
      source_id)` with `source_version`, `external_id`, `last_seen_at`,
      `consecutive_miss_count`, `metadata`
    * `instrument_broker_availability` — one row per `(canonical_symbol,
      broker)` with status in `{unknown, available, unavailable,
      requires_qualification, blocked}`, last-checked timestamps, IBKR
      qualification payload
    * `instrument_source_runs` — audit log of every refresh
  Migration: `alembic/versions/d116a1b2c3d4_instrument_registry.py`.

- Canonical symbol module `instruments/canonical.py` centralises broker
  ↔ canonical translation. `data/universe_builder.py::_to_yf_symbol`
  becomes a thin wrapper.

- Source adapters (`instruments/sources/`) cover four families:
    * `wikipedia.py` (S&P 500 / 400 / 600, Nasdaq-100, Dow 30, FTSE,
      DAX, CAC, Euro Stoxx, Nikkei, TOPIX Core 30, Hang Seng, ASX 200,
      TSX 60 — ~20 indices)
    * `ishares.py` (~40 broad/sector/bond/commodity iShares ETF holdings
      via public CSV endpoints)
    * `openfigi.py` (bulk ISIN/FIGI + alternate-ticker enrichment via
      the OpenFIGI v3 mapping API)
    * `static_fx.py`, `static_futures.py` (G10 FX pairs + CME futures
      roots)
    * `broker_catalog.py` wraps `BrokerAdapter.get_supported_symbols()`
      for every connected adapter — this is how crypto exchanges feed
      the registry.
  All HTTP is funnelled through `instruments/sources/http.py`, a polite
  client with User-Agent, per-host rate limiting, ETag/Last-Modified
  caching, retry with jitter, and timeouts. Each source is fault-
  isolated; one failure cannot taint other sources.

- `instruments/availability.py::resolve_broker_availability(broker,
  adapter)` walks the canonical registry, attempts broker-side
  translation (via `instruments.canonical`), and writes a per-broker
  status row. IBKR uses both the broker catalog and the
  `brokers/ibkr/qualification.py` cache; symbols with no qualification
  record yet are marked `requires_qualification` rather than
  `unavailable`. Operator-pinned and operator-excluded symbols come from
  `config/instrument_registry.yaml::overrides` and become `blocked` or
  `available` regardless of catalog state.

- `instruments/builder.py` orchestrates refresh + availability. Retire
  policy: a symbol is `retired_at` only after
  `consecutive_miss_count >= min_consecutive_misses` (default 5) across
  at least `min_sources_missing` independent sources (default 2). No
  symbol is ever deleted.

- `instruments/scheduler.py` runs four background tasks at boot:
  constituents refresh, broker availability resolution, OpenFIGI
  enrichment, and a connect-event consumer. The scheduler subscribes to
  `BrokerManager.register_connect_callback`, so reconnecting a broker
  (or wiring up a new one) automatically re-evaluates availability for
  every canonical symbol on that broker. `system/orchestrator.py` owns
  the scheduler lifecycle and shuts it down cleanly on stop.

- `brokers/ibkr/adapter.py::get_supported_symbols()` now returns the
  union of (a) the curated YAML seed, (b) the IBKR qualification cache,
  and (c) the D116 registry's `available`/`requires_qualification`
  IBKR rows. Behaviour is gated by
  `IBKR_SUPPORTED_SYMBOLS_USE_REGISTRY` env var or
  `config/instrument_registry.yaml::ibkr_supported_symbols_use_registry`
  feature flag, defaulting to off until the registry is populated.
  Failures fall back silently to the curated YAML — IBKR's effective
  symbol list can only grow, never shrink. `place_order()` still calls
  `qualifyContractsAsync` before submission regardless of source.

- `universe/snapshot_service.py` adds `registry_known_count` and
  `registry_covered_count` per broker to the existing
  `coverage.by_broker` dashboard payload, alongside the broker-catalog
  funnel that drives the headline numbers.

- API additions (read-only): `GET /intelligence/instruments` (summary
  counts by asset class / region / source / broker availability), `GET
  /intelligence/instruments/{canonical}` (registry row + per-broker
  availability + source memberships), `GET /intelligence/instrument-
  sources` (recent run health per source).

- CLI: `python scripts/build_instrument_registry.py --sources=all
  --dry-run` for manual / scheduled refresh; `python
  scripts/qualify_instrument_registry.py --broker=ibkr --limit=100`
  for IBKR contract qualification cache warm-up.

- `config/instrument_registry.yaml` is the single configuration surface:
  enable/disable, IBKR feature flag, retire policy, overrides
  (pinned/excluded), availability timeout, and per-source toggles +
  cadences + sub-source IDs.

What did NOT change: `brokers/base.py`, `risk/engine.py`,
`execution/engine.py`, `signals/*`, `strategies/*`, `portfolio/*`,
`config/ibkr_universe.yaml` (preserved as a curated override layer that
the registry consumer unions in).

Tests:

- `tests/test_instruments_canonical.py` — 13 cases (symbol normalisation
  across equities, FX, crypto, futures, IBKR/Alpaca/Kraken/Binance
  broker translations).
- `tests/test_instruments_registry.py` — 7 cases (coerce_contribution
  with valid/invalid inputs, dataclass guarantees).
- `tests/test_instruments_sources_wikipedia.py` — 4 cases (S&P 500
  fixture parse, error handling, source-id filtering).
- `tests/test_instruments_sources_ishares.py` — 4 cases (CSV parse,
  cash-row filtering, ISIN/sector capture, missing-header errors).
- `tests/test_instruments_sources_openfigi.py` — 4 cases (mapping
  enrichment, empty seed, partial batch).
- `tests/test_instruments_sources_broker_catalog.py` — 5 cases
  (Kraken/Binance symbol normalisation, dedup, adapter failure
  isolation, broker exclusion).
- `tests/test_instruments_availability.py` — 8 cases (alpaca catalog
  resolution, IBKR `requires_qualification`, blocked override,
  end-to-end async resolver with mocked DB).
- `tests/test_instruments_builder.py` — 6 cases (config load defaults,
  YAML overrides, source build filtering, dry-run audit, failure
  isolation).
- `tests/test_instruments_scheduler.py` — 3 cases (broker-connect
  event consumer, async session factory, start/stop lifecycle).
- `tests/test_ibkr_supported_symbols_from_registry.py` — 4 cases
  (curated-seed default, empty-registry fallback, registry union,
  silent recovery from DB error).

Total: 59 D116 tests passing.

Verification:

- `python -m py_compile` for every new module (canonical, registry,
  http, wikipedia, ishares, openfigi, static_fx, static_futures,
  broker_catalog, availability, builder, scheduler, IBKR adapter,
  broker manager, orchestrator, snapshot service, api/server,
  scripts/build_instrument_registry.py,
  scripts/qualify_instrument_registry.py).
- `python -m pytest` D116-scoped suites → `59 passed`.

Rollout:

1. Migration + module + tests land with `enabled: true` but
   `ibkr_supported_symbols_use_registry: false` so behaviour is
   unchanged for IBKR routing.
2. `python scripts/build_instrument_registry.py --sources=all
   --dry-run` confirms source health.
3. First real refresh populates the registry; the orchestrator
   scheduler keeps it up to date.
4. Flipping `ibkr_supported_symbols_use_registry: true` (or the env
   override) grows the IBKR seed beyond the curated 61 names.
5. `tests/test_ibkr_supported_symbols_from_registry.py` guarantees the
   fallback path so IBKR remains routable even if the registry/DB is
   unhealthy.

---

## D115 — Anti-churn + cluster-aware risk + intraday derisk + stale-price gate (2026-05-19)

Five-tier rectification after the 2026-05-19 paper-trading audit. 224 fills
in 8 hours moved $6.0M of turnover on a $1.18M NAV and ended the session
at roughly -$5,000 / -0.4% with no individual stop-loss or daily-loss limit
having fired. The audit traced the bleed to three structural failures
(unbounded duplicate signals, undetected directional clusters, no graduated
portfolio-level defence) and one execution-layer leak (stale-price paper
fills). The risk engine retains unconditional veto power throughout; every
new gate is either a strict reject layer or a reduce-only emitter routed
through the normal SignalEngine + RiskEngine + ExecutionEngine path.

Decision:

- `signals/anti_churn.py` adds an `AntiChurnGate` with three production-
  grade rejects:
    * dedup        — same `(strategy, symbol, side, conf, price)` within
                     90s (per-strategy)
    * contradiction — strategy A long X + strategy B short X within 5min;
                     lower-confidence side rejected, both sides tombstoned
    * post_fill   — re-entry on `(broker, symbol)` within mode-aware
                     cooldown (hunter 120s, trader 180s, defender 600s)
  Wired into `SignalEngine.process()` and `raw_to_signal_candidate()` BEFORE
  meta-label, and `record_fill()` is called by `TradingLoop` after every
  confirmed fill. Operator closes, reduce-only trims, and allocator-
  selected opens are exempt. Config: `config/strategies.yaml::signal_engine.anti_churn`.

- `risk/engine.py::_check_fx_cluster_exposure` caps aggregate signed USD
  exposure across all held forex positions plus the proposed signal.
  Today's six FX legs (EURUSD long, GBPUSD long, AUDUSD long, USDCAD/CHF/
  JPY short) were one bet on dollar weakness sized as if they were six
  independent risks. Pair-orientation rules: xxxUSD long = short USD,
  USDxxx long = long USD. Reduce-only and neutralising legs are never
  blocked. Config: `config/risk_limits.yaml::fx_cluster`.

- `risk/engine.py::_check_equity_index_cluster_exposure` is the symmetric
  cap for the US broad-market index family (SPY, QQQ, IWM, DIA, VTI, VOO,
  IVV, MDY, TQQQ/SQQQ/SPXL/SPXS). Same neutralise/reduce-only rules.
  Config: `config/risk_limits.yaml::equity_index_cluster`.

- `risk/intraday_derisk.py` implements a graduated portfolio-level defence
  that sits BEFORE the static `max_daily_loss_pct` kill switch. Three
  tiers: -0.5% intraday triggers a 20% trim of the worst losers (max 2),
  -1.0% triggers a 50% trim (max 4), -1.5% triggers a full close (max 6).
  Cooldown 120s per `(broker, symbol)`. Wired as
  `Orchestrator._intraday_derisk_loop` / `_run_intraday_derisk_tick`,
  cancelled on stop. All emitted actions are reduce-only, still routed
  through SignalEngine + RiskEngine + ExecutionEngine. Config:
  `config/risk_limits.yaml::intraday_derisk`. Profit-harvest peak
  persistence (D115 item 8) was already in place via
  `Orchestrator._persist_profit_harvest_peaks` and survives restart.

- `execution/engine.py::_simulate_fill` rejects an opening paper fill when
  `signal.suggested_price` has drifted against the trade direction by
  more than `stale_price_gate.max_adverse_drift_bps` (default 25 bps).
  Returns a REJECTED OrderResult with `filled_quantity=0` and sets
  `last_skip_reason`. Reduce-only / close intents are exempt. Config:
  `config/risk_limits.yaml::stale_price_gate`. Backtest harness disables
  this and the anti-churn gate for the duration of the run (wall-clock
  semantics do not apply when replaying historical bars in milliseconds).

- `scripts/flatten_orphaned_remnants.py` is the operator-facing housekeeping
  tool for the post-incident cleanup. Identifies and (with `--apply`)
  flattens paper-ledger positions below a configurable notional ceiling
  (default $25,000) with optional symbol/broker/loss-pct filters. Paper-mode
  only; refuses live. Dry-run by default. Writes filled close `OrderLog`
  rows plus zero-quantity `PositionLog` tombstones using the same helper
  that backs the D070 local-paper-flatten path.

Tests:

- `tests/test_anti_churn_gate.py` — 17 cases (dedup, contradiction,
  post-fill cooldown, mode-aware cooldowns, operator-close exemption,
  engine integration).
- `tests/test_fx_cluster_exposure.py` — 9 cases (orientation helper, signed
  exposure math, cap enforcement, neutralising / reduce-only / non-FX
  exemptions, disabled-gate passthrough).
- `tests/test_equity_index_cluster.py` — 6 cases (cluster membership,
  additive vs opposite-direction, reduce-only, disabled-gate).
- `tests/test_intraday_derisk.py` — 11 cases (no-action when positive,
  tier ladder, short-position close direction, cooldown, min-loss filter,
  winners never trimmed).
- `tests/test_stale_price_gate.py` — 7 cases (BUY/SELL adverse rejection,
  favorable drift fill, sub-threshold fill, reduce-only exemption,
  disabled-gate passthrough, missing-quote passthrough).
- `tests/test_flatten_orphaned_remnants.py` — 5 cases (loss-pct math).

Verification:
- `python -m py_compile signals/anti_churn.py signals/engine.py risk/engine.py risk/intraday_derisk.py execution/engine.py system/orchestrator.py system/trading_loop/loop.py scripts/flatten_orphaned_remnants.py backtest/harness.py`
- `python -m pytest -q` → `1235 passed, 3 skipped, 1 warning` (the warning
  is a pre-existing AsyncMock fixture leak in `test_profit_harvest.py`,
  unchanged by this work).
- Targeted: anti-churn / FX cluster / equity-index cluster / intraday
  derisk / stale-price gate / remnants suites → `55 passed`.

## D114 — Session-exit policy embedded in global-edge decisions (2026-05-19)

Market-session intelligence now includes pre-close position review without
becoming a blunt "close everything at the bell" rule.

Decision:
- `core/market_session.py` exposes `session_close_at()` and
  `minutes_to_session_close()` as broker-aware timing helpers. 24/7 venues and
  unknown/closed markets return `None` so no synthetic close is invented.
- `config/session_exit_policy.yaml` defines the governed pre-close windows and
  mode/horizon policy. Intraday/scalp positions default to no overnight carry;
  swing/position trades default to holding through the close.
- `core/session_exit_policy.py` evaluates each position into one of:
  `hold_through_close`, `trim_before_close`, `close_before_close`, or
  `defer_action`, with an explicit reduce fraction and reason.
- `GlobalEdgeCoordinator.propose_session_exit_actions()` converts only
  executable close/trim decisions into normal `trim_symbol` reduce-only
  coordinator actions. These still pass through SignalEngine, RiskEngine,
  router, and ExecutionEngine; there is no risk bypass and no forced flatten.
- `TradingLoop._run_global_edge_tick()` prepends session-exit reduce-only
  actions ahead of ordinary opens/replacements, de-duplicating against other
  reduce actions already selected in the same loop.

Implication:
The system can close or trim positions before a venue shuts only when the
position's own profile says that is appropriate: explicit intraday/no-overnight
positions close near the bell; defender mode can bank profitable swing exposure;
normal swing/position theses can remain open overnight/days/weeks. The
execution gate remains the final binary physics check for closed venues.
