# myTbot Product Specification and Technical Architecture

Last updated: 2026-05-20

Audience: business users, operators, product stakeholders, engineers, data/AI teams, and risk reviewers.

Status: living specification for the current myTbot platform. This document describes the implemented platform and the intended operating model. It is not investment advice and does not grant authority for unmanaged live capital deployment.

---

## 1. Executive Summary

myTbot is a personal autonomous multi-asset trading platform. It connects to multiple brokers and data sources, builds a dynamic investable universe, generates trading opportunities from quantitative, news, macro, volume, and arbitrage signals, routes every trading intent through a veto-capable risk engine, and executes approved orders through broker adapters.

The platform is designed as a "one-button" system:

- `python run.py` starts the backend, dependencies, broker discovery, trading loop, data pipeline, API, WebSocket, and dashboard service.
- The UI exposes a single live control plane: start, stop, capital allocation, mode/status, Connect Hub, Universe intelligence, performance, and trading/risk visibility.
- AI is local-first and advisory. AI can classify, score, summarize, explain, escalate, veto before risk under configured policy, and enrich signal conviction. AI cannot place orders or bypass the risk engine.

**Core business promise:** give the operator a continuously adapting trading system that can monitor many assets and brokers, decide where capital is best deployed, protect against common live-trading failure modes, and keep the operator informed in plain language.

**Core technical promise:** every external venue is isolated behind adapters, every order passes through risk, every important decision is auditable, and most behaviour is config-driven rather than hard-coded.

---

## 2. Platform Goals

### Business Goals

- Manage a personal multi-asset portfolio across equities, ETFs, bonds, forex, crypto, and optional single-leg options.
- Make capital allocation dynamic: capital should move toward the best current opportunity, not sit inside fixed strategy buckets.
- Use AI to interpret news, macro context, disagreement, and unusual events without letting AI become an uncontrolled trading actor.
- Provide visibility into why the system is trading, waiting, trimming, or de-risking.
- Support gradual activation: paper mode, micro-live guardrails, live capital scaling, and connector onboarding.

### Technical Goals

- Maintain a single broker abstraction in `brokers/base.py`.
- Keep strategy, signal, risk, execution, portfolio, and UI concerns separated.
- Use deterministic fallbacks when AI, broker, or data dependencies are unavailable.
- Persist operational state, decisions, order/fill logs, P&L, universe state, and learned routing/universe state.
- Allow new brokers, feeds, AI providers, and treasury accounts to be represented through Connect Hub.

---

## 3. Primary Users and Use Cases

### Operator / Business User

Uses the UI to answer:

- Is the system running?
- How much NAV is available?
- How much capital is actually deployed?
- Which brokers are connected and balance-ready?
- What is the current performance, drawdown, Sharpe, and deployment?
- What is the system watching in the Universe?
- Why is it not deploying more capital?
- Which connectors, AI providers, and feeds are configured?
- Is risk limiting or stopping the system?

### Technical / Quant Team

Uses the platform to:

- Add strategies and feature sources.
- Tune config-driven guardrails.
- Review signal/risk/execution logs.
- Add broker adapters.
- Add AI providers or information feeds.
- Extend universe discovery, allocator logic, and model governance.

### Risk / Operations Reviewer

Uses the platform to:

- Confirm no order path bypasses risk.
- Audit kill switch, broker coverage, cluster caps, stale-price gates, drawdown monitors, and de-risk actions.
- Validate live readiness and connector permissions.
- Review whether AI has advisory-only boundaries.

---

## 4. Product Surfaces

### Dashboard

The main dashboard shows:

- Net asset value.
- Realised P&L by today, week, month, YTD, and historical windows.
- Performance strip: return, Sharpe, max drawdown, deployment.
- Capital allocation slider.
- Capital at work and free to deploy.
- Conviction river.
- Live feed of events, orders, signals, and system activity.
- Current positions, orders, approvals, rejections, and execution issues.

Important distinction:

- **Capital allocation slider** is the ceiling: how much NAV the operator allows the system to use.
- **Deployment / at work** is actual capital deployed now. It can be lower than the ceiling if there are not enough approved opportunities.

### Capital Allocation

The capital slider controls the global deployment ceiling. At `100%`, the system may use the full NAV as its capital budget, subject to risk, opportunity quality, session, broker, minimum order, and execution gates. It does not force immediate full deployment.

Capital-at-work is calculated in cash-deployed terms, not always gross notional:

- Equity, ETF, crypto spot: usually 1.0x cash factor.
- Forex: margin-style cash factor.
- Futures: margin-style cash factor.
- Bonds: discounted cash factor.
- Options: premium-style exposure.

### Universe UI

The Universe UI explains how the platform turns broker listings and registry entries into the smaller active set used by the data pipeline and allocator.

Stages:

- Broker listings.
- Unique normalized symbols.
- Priority ranked.
- Scored.
- Watching.
- Promoted overlay.
- Active representatives.

It also shows:

- Adaptive universe caps.
- Self-tuning priority rule.
- Asset-class coverage.
- Correlation clusters.
- Tier transitions.
- Instrument cards/list with last-scored metadata.

### Connect Hub

Connect Hub manages external systems:

- Brokers and trading venues.
- Information feeds.
- AI providers.
- Treasury/funding accounts.

It reports:

- Enabled/configured/connected/healthy.
- Required environment variables, without secret values.
- Capabilities and safety policies.
- Next actions.

Treasury is currently conservative: balance/read metadata can be represented, but automatic fund movement is disabled unless a future approval workflow is explicitly built.

---

## 5. Trading Modes

The platform exposes the conceptual modes `hunter`, `trader`, and `defender`.

Current implementation state:

- The active default is `hunter`.
- Mode selection is intended to be adaptive rather than manually switched.
- Runtime manual mode switching is disabled by policy in `config/profile_modes.yaml`.
- The three mode blocks currently alias the same canonical policy shape. Live behavioural differences are increasingly computed by adaptive modules rather than static per-mode YAML values.

### Hunter

Business meaning: aggressive, opportunity-seeking, faster rotation, higher willingness to deploy when the evidence is strong.

Technical role:

- Current canonical baseline.
- Used for action cadence and default aggressive policy floors.
- Adaptive layers refine sizing, cadence, edge threshold, and strategy weights.

### Trader

Business meaning: balanced mode between opportunity capture and risk control.

Technical role:

- Present as a product and compatibility label.
- Currently aliases the canonical mode block.

### Defender

Business meaning: risk-off posture, slower, more protective.

Technical role:

- Present as a product and compatibility label.
- De-risk, profit harvest, drawdown, and safety monitors still enforce defensive behaviour when actual risk conditions require it.

### Paper, Micro-Live, and Live

- Paper mode is the normal proving ground. Orders are simulated or routed to paper/test environments.
- Micro-live mode uses tight guardrails, whitelists, broker permissions, and small notional exposure.
- Live mode connects to real broker environments and requires stronger operational discipline.

---

## 6. Strategy Suite

Strategies emit candidate intent. They do not place orders directly.

### Directional / Quant Strategies

- `momentum_breakout`: looks for momentum continuation with ATR and volume filters.
- `mean_reversion`: looks for oversold/overbought reversals using RSI/band behaviour.
- `volume_flow`: detects volume anomalies, flow persistence, and possible exhaustion.
- `volatility_regime`: reacts to volatility expansion/compression and regime state.
- `regime_rotation`: rotates between risk-on and risk-off anchor assets.
- `pairs_trading`: trades relative spread opportunities across configured pairs.

### **AI-Enabled** Event Strategy

- `event_driven_news`: uses news shocks and AI-classified event context to generate or modify trading intent.
- News impact can affect sentiment, materiality, confidence, and urgency.

### Arbitrage Strategies

- `funding_rate_arbitrage`: seeks spot/perpetual funding carry where venue support exists.
- `cross_exchange_arbitrage`: detects spread opportunities between venues after fee, slippage, latency, and liquidity checks.

### Optional / Advanced Strategy Families

- Factor sleeve scoring: value, quality, momentum, defensive, carry families.
- Statistical arbitrage pairs.
- Options directional strategies: long calls/puts.
- Options hedging strategies: protective puts and covered calls.

Options are opt-in and heavily guarded. Current config restricts options to paper-only and limited underlyings/premium unless changed.

---

## 7. Signal and Opportunity Architecture

### Raw Signal

Strategies produce raw directional ideas with:

- Symbol.
- Side.
- Confidence.
- Suggested price/quantity/notional.
- Strategy metadata.
- Optional sizing intent.

### Signal Engine

The signal engine normalizes raw strategy output into a unified `Signal`.

It handles:

- Confidence normalization.
- Volatility-aware sizing.
- AI/news score adjustment.
- Dual AI veto policy.
- Meta-label filtering.
- Anti-churn protection.
- Accumulator integration.

### **AI-Enabled** Signal Accumulation

`signals/accumulator.py` maintains stateful, time-decayed conviction per symbol.

Sources:

- Quant strategy events.
- AI news rollups.
- Macro regime.
- Volume/flow signals.

Techniques:

- Half-life decay.
- Short/medium/long score buckets.
- Alignment bonus when sources agree.
- Conflict penalty when sources disagree.
- Bounded net score.

Business benefit: the system avoids treating every signal as isolated. Conviction can build or fade over time.

### Meta-Labeling

The meta-label layer filters weak or poorly aligned candidates before allocator/risk/execution.

Current behaviour:

- Heuristic meta-labeling is live.
- Trained model support exists behind governance gates.
- Model governance is required before a trained meta-labeler becomes authoritative.

---

## 8. D015 Global Opportunity Replacement Allocator

The allocator changes the system from "strategy sleeves spend their own budgets" to "all capital continuously competes for the best opportunity."

Business rule:

> Capital should be held by the strongest current opportunity, not by the oldest or most convenient position.

Core concepts:

- New opportunities are ranked by expected edge.
- Held positions are scored by expected remaining edge.
- A new opportunity can displace an old position if it clears switching costs and churn penalties.
- Capital can be reused by trimming, closing, or rotating existing positions.
- Free cash is not strictly required; the system may reduce weaker holdings to fund stronger ones.

Inputs:

- Strategy opportunity scores.
- Market state.
- Volume anomalies.
- News impact.
- Regime alignment.
- Liquidity quality.
- Structure quality.
- Relative strength.
- Held-position score.
- Exit pressure.
- Switching cost.

Outputs:

- Open/increase actions.
- Reduce/trim actions.
- Rotation actions.
- Soft-shed actions if capital ceiling is lowered.
- Capital recycle actions for winners or dead-edge holdings.

Every allocator action still becomes a normal signal and passes through risk and execution.

---

## 9. Universe and Instrument Intelligence

### Instrument Registry

The instrument registry is a master catalog of canonical instruments and broker availability.

Sources include:

- Wikipedia index constituents.
- iShares ETF holdings.
- OpenFIGI enrichment.
- Static FX/futures lists.
- Broker catalogs.

It stores:

- Canonical symbol.
- Asset class.
- Region/exchange/currency.
- Sector/industry.
- ISIN/FIGI.
- Source memberships.
- Broker availability.

### Dynamic Universe

The universe builder collects broker-listed and registry-known symbols, normalizes them, and creates tiers.

The current D118 funnel is:

1. Broker listings.
2. Unique normalized.
3. Priority ranked.
4. Scored.
5. Watching.
6. Promoted overlay.
7. Active representatives.

### Self-Tuning Priority Pre-Filter

The D118 priority rule scores symbols before expensive feature/scoring work.

Components:

- Liquidity prior.
- Anchor pin.
- Freshness bonus.
- Registry availability.
- Asset-class balance.
- Region balance.

Self-tuning behaviour:

- Online logistic regression learns component weights.
- AdaGrad and EWMA decay stabilize learning.
- Budget controller uses AIMD and utility saturation.
- Score-age state tracks last scored, score count, and first seen.
- Tier transitions are persisted for auditability.

Business benefit: the system can watch a broad universe without wasting resources on redundant or stale symbols.

### Adaptive Universe Caps

D117 adapts the discovery net:

- Wider in risk-on, trend-up, volatile, or high signal-pressure regimes.
- Narrower in risk-off, crash, range, or low signal-pressure regimes.
- Cluster-aware floors ensure enough coverage when more independent opportunity clusters exist.
- Anti-churn hysteresis prevents one-cycle liquidity noise from dropping useful names.

---

## 10. Risk Engine

The risk engine is the unconditional authority. No order bypasses it.

### Pre-Trade Risk

Checks include:

- Minimum confidence.
- Trade quality score.
- Static exposure caps when enabled.
- Asset class limits when enabled.
- Order notional and liquidity limits.
- Spread/slippage limits.
- Proportionality/minimum-order gates.
- Broker disabled/coverage gates.
- Options-specific guardrails.

### Portfolio Risk

Checks include:

- Daily loss limit.
- Drawdown from high-water mark.
- Consecutive loss cooldown.
- Portfolio gross/net exposure.
- Concentration and cluster checks.

### D115 Cluster Caps

FX cluster:

- Aggregates signed USD directional exposure across forex pairs.
- Prevents many FX positions from becoming one hidden USD bet.

Equity index cluster:

- Aggregates broad US equity beta across SPY/QQQ/IWM/DIA/VTI/VOO/IVV/MDY and leveraged variants.
- Prevents multiple index ETFs from bypassing systematic exposure limits.

### Intraday De-Risk

Graduated action before the hard daily-loss limit:

- Light trim at early loss thresholds.
- Larger trims at deeper loss thresholds.
- Full closes of worst losers at severe thresholds.

All actions are reduce-only and still pass through risk and execution.

### Stale-Price Gate

Paper fills are rejected if an opening signal's suggested price is stale and the broker quote has moved against the trade by more than the configured adverse drift threshold.

### Profit Harvest

The profit harvester monitors open positions and can emit reduce-only trims or closes when:

- Profit exceeds volatility-adjusted thresholds.
- Trailing giveback is reached.
- Full-close profit thresholds are met.

### Stop-Loss Monitor

The orchestrator runs a stop-loss monitor that emits reduce-only close signals when position loss conditions are met. These closes still pass through risk and execution.

### Kill Switch

The kill switch can be activated via API/UI and cannot auto-reset. It is intended for emergency halt conditions.

---

## 11. Execution Engine and Routing

Execution happens only after risk approval.

### Execution Flow

1. Signal approved by risk.
2. Execution planner builds order intent.
3. Smart router selects broker/venue.
4. Execution engine places the order using idempotency keys.
5. Fill tracker updates orders, fills, positions, and daily P&L.
6. Reconciliation compares broker state to internal state.

### Smart Routing

Routing considers:

- Broker support for the asset.
- Fees.
- Learned execution quality.
- Slippage and fill history.
- Broker availability and balance readiness.
- Broker permission/fallback map.

### Broker Adapters

Implemented/represented broker classes include:

- IBKR: equities, ETFs, bonds, forex, and opt-in single-leg options.
- Kraken: crypto spot.
- Binance: crypto spot.
- Bybit: spot/USDT linear support.
- Alpaca: equities/crypto where configured.

New brokers should implement `brokers/base.py` and register in `brokers/registry.py`.

---

## 12. **AI-Enabled Features**

AI is one of the platform's major differentiators, but it is bounded by design.

### Local-First AI Stack

Configured path:

1. Rules engine.
2. FinBERT sentiment model.
3. Local LLM via Ollama.
4. Optional premium fallback provider.

Current config:

- FinBERT: `ProsusAI/finbert`.
- Local primary: `qwen2.5:7b`.
- Local fallback/ensemble: `llama3.1:8b`.
- Premium fallback: Anthropic `claude-sonnet-4-5`, enabled but necessity-gated.

### **AI-Enabled** News Intelligence

AI classifies:

- Event type.
- Affected symbols.
- Sentiment.
- Materiality.
- Credibility.
- Novelty.
- Macro relevance.

Outputs feed:

- Event-driven news strategy.
- Signal accumulator.
- Demand engine.
- News veto policy.
- Dashboard explanations and audit logs.

### **AI-Enabled** Macro and Regime Context

The AI pipeline consumes macro series such as:

- Federal funds rate.
- CPI.

It can classify macro regimes and gate strategies by regime policy.

### **AI-Enabled** Escalation

Escalation is necessity-based, not budget-cap based.

Escalation signals include:

- Ambiguity.
- Materiality.
- Novelty.
- Provider disagreement.
- Local LLM ensemble disagreement.
- Emergency keywords such as flash crash, market halt, bank failure, exchange hack, sovereign default.

### **AI-Enabled** Anomaly Detection

The AI pipeline can flag:

- Low-confidence classification.
- High disagreement.
- High-impact unusual events.
- Macro/news conflicts.

### **AI-Enabled** Rationale and Audit

AI outputs are persisted with:

- Provider name.
- Latency.
- Cost estimate.
- Classification output.
- Failed outputs where configured.
- Trade rationale where available.

### AI Safety Boundary

AI does not:

- Place orders.
- Call broker APIs.
- Override risk rejections.
- Change capital allocation without policy.
- Bypass the operator kill switch.

AI may:

- Score.
- Explain.
- Enrich.
- Escalate.
- Veto before risk under configured signal policy.

---

## 13. Data Pipeline and Feature Store

### Market Data

Data source is currently yfinance for pipeline OHLCV, with broker data also used at runtime.

Pipeline intervals:

- Incremental: 1 hour.
- Backfill: daily over approximately 2 years.
- Historical training backfill: 1 hour over approximately 730 days.

### Features

Feature categories include:

- RSI, MACD, ATR, momentum.
- Volume z-score.
- Dollar volume.
- Volume persistence.
- Trade count proxy.
- Fake-spike penalty.
- VPIN-style features.
- Fractional differencing.
- Hurst.
- GARCH where available.
- Regime and cross-section metrics.

### News and Macro

Feeds include:

- NewsAPI.
- Alpha Vantage.
- Finnhub.
- Marketaux.
- FRED.

News is deduplicated and passed into the AI pipeline.

---

## 14. Connectors and Treasury

### Connector Types

Connect Hub supports:

- Brokers.
- Information feeds.
- AI providers.
- Treasury accounts.

Each connector declares:

- Auth type.
- Required secrets.
- Capabilities.
- Roles.
- Safety policy.
- Notes/docs.

### Treasury Model

Treasury accounts are intentionally conservative.

Current supported concept:

- Read/represent external balances.
- Display funding source status.
- Generate manual funding recommendations.

Current default:

- Automatic cash movement disabled.
- Transfer quotes approval-only.
- Beneficiary whitelist required before any future movement.
- Manual approval above zero.
- Full audit trail required.

Wise and Kraken can be represented differently:

- Wise: likely fiat treasury source, but direct personal-account API automation may be limited.
- Kraken: strong crypto/stablecoin treasury venue, not a replacement for bank-like fiat treasury.

---

## 15. API and Runtime Control Plane

Important API surfaces:

- `POST /system/start`
- `POST /system/stop`
- `GET /system/status`
- `GET /dashboard/snapshot`
- `GET /pnl`
- `GET /pnl/history`
- `GET /pnl/realised-curve`
- `GET /intelligence/universe`
- `GET /intelligence/instruments`
- `GET /connect/hub`
- `POST /connect/add`
- `POST /connect/configure`
- `POST /connect/enable`
- `POST /connect/delete`

WebSocket:

- Emits live ticks, status, events, and dashboard invalidation hints.

Control security:

- Mutating endpoints can be guarded by `X-Control-Token` / `API_CONTROL_TOKEN`.
- Read tokens/passwords can be enabled for dashboard read paths.
- Secrets are stored in `.env` and never returned by Connect Hub.

---

## 16. Storage and Auditability

The platform persists:

- Orders.
- Fills.
- Signals.
- Risk decisions.
- Daily P&L.
- Feature snapshots.
- AI outputs.
- Parameter logs.
- Control state.
- Dashboard snapshots.
- Universe state.
- Instrument registry.
- Routing quality state.

Audit principle:

> If the system made a trading-relevant decision, the operator should be able to reconstruct what happened, what inputs were available, and which gate approved or rejected it.

---

## 17. Technical Architecture

### Runtime Stack

- Python 3.12+ backend.
- FastAPI API server.
- React/Vite frontend.
- PostgreSQL/TimescaleDB.
- Redis.
- Docker Compose for dependencies.
- SQLAlchemy.
- asyncio/aiohttp.
- Loguru.

### Main Components

```text
run.py
  -> system/orchestrator.py
      -> dependency manager
      -> broker manager
      -> trading loop
      -> data pipeline runner
      -> risk monitors
      -> API/WebSocket state

data/
  -> prices, features, universe pre-filter, registry sources

strategies/
  -> candidate signal generation

signals/
  -> signal engine, accumulator, anti-churn, meta-labeling, opportunity bridge

portfolio/
  -> D015 allocator, global edge coordinator, treasury manager

risk/
  -> risk engine, stop loss, de-risk, parameter manager

execution/
  -> planner, router, execution engine

brokers/
  -> adapters for IBKR, Kraken, Binance, Bybit, Alpaca

api/
  -> FastAPI endpoints

ui/
  -> operator dashboard
```

### Decision Pipeline

```text
Market/news/macro data
  -> feature store
  -> strategy candidates
  -> AI/news/macro enrichment
  -> signal accumulator
  -> meta-label filter
  -> opportunity allocator/global edge coordinator
  -> risk engine
  -> execution planner/router
  -> broker adapter
  -> fills/reconciliation
  -> P&L/dashboard/audit logs
```

### Import Direction

Core flow is one-way:

```text
data -> strategies -> signals -> risk -> execution -> portfolio/UI
```

Risk should not depend on strategies. Brokers should not contain business logic.

---

## 18. Safety and Governance Principles

- Risk engine is law.
- AI advises, rules execute.
- Paper first, micro-live second, live scale later.
- Broker adapters are isolated.
- Everything important is logged.
- Decimal is preferred for money.
- Broker coverage affects risk routing.
- Connector deletion never silently deletes credentials.
- Treasury movement is disabled until governed approvals exist.

---

## 19. Operational Runbook Summary

### Start

```bash
python run.py
```

The orchestrator:

- Starts/checks Postgres and Redis.
- Discovers brokers.
- Starts the trading loop.
- Runs data pipeline tasks.
- Publishes dashboard/API/WebSocket state.

### Stop

Use UI or:

```http
POST /system/stop
```

The orchestrator drains background tasks and persists final NAV/P&L where possible.

### Health Checks

Review:

- `/system/status`
- `/dashboard/snapshot`
- `/pnl`
- Connect Hub.
- Broker balance-ready status.
- UI live feed.

### Common Operator Questions

Why is allocation 100% but deployment lower?

- Allocation is a ceiling.
- Deployment is actual capital at work.
- If there are no approved candidates, deployment remains below ceiling.

Why did the system not trade?

- Possible reasons: no batch candidates, signal below threshold, risk veto, stale data, broker disabled, session closed, anti-churn gate, no route, insufficient liquidity, or capital already better deployed.

Why did the system trim/close?

- Possible reasons: profit harvest, stop loss, intraday de-risk, aggregate de-risk, session exit policy, capital recycle, soft-shed after allocation reduction, or replacement by stronger opportunity.

---

## 20. Known Constraints and Current Caveats

- Futures are currently useful for data/universe context, but execution depends on contract resolver support.
- Options are opt-in, paper-only by default, and tightly capped.
- Wise Personal may not support the desired direct API treasury automation; business API or open banking aggregator may be required.
- Kraken can be a crypto/stablecoin treasury venue, but not a clean bank-like fiat treasury replacement.
- Sharpe and other performance metrics are only as reliable as the available NAV history window.
- Static mode labels remain, but adaptive modules now compute much of the practical behaviour.
- Trained meta-labeler support exists, but model governance gates determine whether it should be authoritative.

---

## 21. Roadmap Candidates

Business/product:

- Treasury dashboard with Wise/Kraken roles separated.
- Manual funding recommendation workflow.
- Human approval queue for treasury transfer quotes.
- Trade-detail rationale page for every fill.
- Better dashboard distinction between allocation ceiling, cash deployed, notional gross, and free-to-deploy.

Technical:

- Open Banking aggregator for personal fiat treasury read access.
- IBKR futures contract resolver.
- More complete trained-model governance workflow.
- Additional broker adapters.
- Enhanced model monitoring for AI drift/disagreement.
- Deeper performance attribution by strategy, asset class, broker, and AI involvement.
- Automated documentation extraction from config and OpenAPI schemas.

---

## 22. Key File Map

Runtime:

- `run.py`
- `system/orchestrator.py`
- `system/trading_loop/`
- `api/server.py`

Broker/connectors:

- `brokers/base.py`
- `brokers/registry.py`
- `system/broker_manager.py`
- `system/connect_hub.py`
- `connectors/base.py`
- `config/connectors.yaml`

AI:

- `config/ai.yaml`
- `ai/router.py`
- `ai/escalation.py`
- `ai/providers/`
- `ai/pipeline.py`
- `signals/accumulator.py`

Strategies/signals:

- `config/strategies.yaml`
- `strategies/`
- `signals/engine.py`
- `signals/anti_churn.py`
- `signals/meta_labeler.py`

Allocator/portfolio:

- `config/allocation.yaml`
- `config/global_edge.yaml`
- `portfolio/allocation_engine.py`
- `portfolio/global_edge_coordinator.py`
- `portfolio/treasury_manager.py`

Risk/execution:

- `config/risk_limits.yaml`
- `risk/engine.py`
- `risk/intraday_derisk.py`
- `risk/stop_loss.py`
- `execution/engine.py`
- `execution/router.py`
- `execution/planner.py`

Universe/instruments:

- `config/data_pipeline.yaml`
- `config/instrument_registry.yaml`
- `data/universe_prefilter.py`
- `data/universe_weight_learner.py`
- `data/universe_budget_controller.py`
- `data/universe_transitions.py`
- `universe/snapshot_service.py`
- `instruments/`

UI:

- `ui/src/app/redesign/`
- `ui/src/app/lib/api.ts`

Storage:

- `storage/models.py`
- `alembic/`

---

## 23. Glossary

- **NAV:** Net asset value across included, balance-ready brokers.
- **Allocation ceiling:** Operator-selected maximum percentage of NAV the system may deploy.
- **Deployment / capital at work:** Actual cash-equivalent capital currently deployed or reserved.
- **Gross exposure:** Absolute notional exposure.
- **Net exposure:** Directional net exposure after longs/shorts.
- **Signal:** Unified trade intent after strategy normalization.
- **Risk veto:** Risk engine rejection that prevents execution.
- **Accumulator:** Stateful conviction memory by symbol.
- **Universe:** The set of instruments currently considered for data/scoring/trading.
- **Active representative:** A non-redundant symbol representing a correlation cluster.
- **Connector:** External system represented in Connect Hub.
- **Treasury:** External funding or reserve account, not necessarily a trading venue.
- **D015:** Global opportunity replacement allocator.
- **D116:** Instrument registry and broker availability resolver.
- **D117:** Adaptive universe tier sizing.
- **D118:** Self-tuning priority pre-filter and universe funnel.
