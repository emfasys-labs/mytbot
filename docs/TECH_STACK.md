# TECH_STACK.md
# ==============
# Every technology choice with justification.
# Updated to incorporate: "Predictive and decision-making technologies
# for multi-asset algorithmic trading" research report.

---

## Language & Runtime

| Technology | Version | Why |
|-----------|---------|-----|
| Python | 3.12+ | Best quant/finance ecosystem. All statistical, ML, broker libraries native. Async for WebSocket. |
| asyncio | stdlib | All broker calls async. Concurrent WebSocket streams without threading. |
| aiohttp | 3.9+ | Async HTTP client for REST API calls. |

---

## Broker SDKs

| Library | Broker | Why |
|---------|--------|-----|
| ib_insync | IBKR | Best IBKR Python library. Async support. Note: research report mentions `ib_async` as potential rename — monitor. |
| python-kraken-sdk | Kraken | Official Kraken SDK. REST + WebSocket. |
| python-binance | Binance | Most mature Binance library. Rate limit handling built in. |
| pybit | Bybit | Official-style V5 unified trading REST/WebSocket client. |
| alpaca-py | Alpaca | Official Alpaca SDK. Paper/live toggle. Best paper trading environment. |
| ccxt | Multi-exchange | 107+ crypto exchanges unified. Essential for funding rate data and cross-exchange arbitrage. |

---

## Data & Market Data

| Library | Why |
|---------|-----|
| pandas + numpy | Core data manipulation. OHLCV, feature engineering, rolling windows. |
| yfinance | Historical data for research/backtest only. Not production. |
| fredapi | Free Federal Reserve macro data. Interest rates, CPI, GDP, employment. Essential for bond/forex signals. |
| edgartools | SEC EDGAR filings. Year-over-year text similarity in risk factors correlates with future returns (M6). |
| praw | Reddit sentiment. Best used as contrarian indicator — extreme bullish precedes corrections (M6). |

**Data provider hierarchy (from research):**
- Polygon.io (rebranded "Massive") — developer-friendly, SIP feed, tiered pricing starting free
- Databento — modern standard for systematic trading, 60+ venues, usage-based from $125
- FRED — free macro data
- Benzinga — accessible news, free basic tier (130-160 articles/day)
- Bloomberg/RavenPack — enterprise only, not needed at our scale

---

## Technical Indicators

| Library | Why |
|---------|-----|
| pandas-ta | 150+ indicators. Pure Python, easiest install. Good for development. |
| TA-Lib | C-based, 200+ indicators, fastest. Use in production. Requires system install. |

**Evidence ratings from research (use as features in ML, not standalone signals):**
- RSI (14-period): moderate-strong evidence as mean-reversion signal
- ATR: strong evidence for position sizing and stops — weak as standalone signal
- VWAP: strong for intraday institutional support/resistance
- MACD alone: poor — use combined with momentum indicators
- Bollinger Bands: ~14.7% feature importance in Random Forest — generates false signals in trends
- Best MA crossover: 13-day and 48.5-day EMA (300 years of data, 16 global indices)

---

## Statistical Models (M2) — from research report

| Library | Method | Evidence | Use case |
|---------|--------|----------|----------|
| statsmodels | ARIMA/SARIMA | Weak equities, moderate macro | Macro series only — interest rates, CPI, not equity returns |
| pmdarima | auto_arima | Same | Automatic order selection |
| statsmodels | Cointegration (Johansen) | ★★★ moderate | Pairs trading ETF pairs: TLT/IEI, EWA/EWC. Half-life 5-10 days. |
| arch | GARCH family | ★★★★★ strong | Volatility forecasting 1-10 days. VaR estimation. |
| pykalman | Kalman filter | ★★★ moderate | Dynamic hedge ratios for pairs. Real-time spread tracking via Redis pub/sub. |
| fracdiff | Fractional differencing | ★★★★★ CRITICAL | MANDATORY for all ML features. d≈0.12-0.43, preserves >90% correlation with original. |
| nolds | Hurst exponent (DFA) | ★★★★ strong | Regime classification: H<0.5=mean-revert, H=0.5=random walk, H>0.5=trending |

**GARCH variant by asset class:**
| Asset | Variant | Rationale |
|-------|---------|-----------|
| Equities/indices | GJR-GARCH(1,1) | Captures leverage effect |
| Forex | EGARCH or GARCH(1,1) | Moderate asymmetry |
| Commodities | EGARCH | Asymmetric supply shocks |
| Crypto | EGARCH | Extreme asymmetry, rapid regimes |
| Multi-asset portfolio | DCC-GARCH | Time-varying correlations |

---

## ML & Strategy (M3) — enhanced from research report

| Library | Why |
|---------|-----|
| catboost | Best accuracy on tabular financial data. CatBoost ≈ XGBoost > LightGBM > Random Forest. |
| xgboost | Strong performer. Standard for tabular financial data. |
| lightgbm | Fastest training. Use when XGBoost too slow at scale. |
| scikit-learn | Baseline. Random Forest recommended first — variance (overfitting) more dangerous than bias in finance. |
| optuna | Bayesian hyperparameter optimisation. Sample from log-uniform distributions. |
| shap | Feature importance. Use MDI + MDA + SHAP combined. Never rely on one method alone. |
| mlfinlab | López de Prado: triple barrier labeling, meta-labeling, VPIN, sample uniqueness weighting. |
| timeseriescv | **MANDATORY.** Purged + Combinatorial CV. Standard k-fold massively overstates performance. |

**Critical hyperparameters (aggressive regularisation required in finance):**
- Learning rate: 0.01–0.05, never >0.1
- Max depth: 3–6 (shallow trees for low signal-to-noise)
- Subsample/feature fraction: 0.5–0.8
- L1/L2 regularisation: 0.01–10.0 (log-uniform search)
- Early stopping: 50–100 rounds on purged validation

**Meta-labeling pattern (research-validated):**
1. High-recall primary model predicts direction (momentum rules)
2. Secondary ML model predicts whether to take the trade
3. Meta-model predicted probability = bet size
4. This is how ML adds real value — filtering false positives, not raw prediction

**Triple barrier method for labels:**
- Profit-taking barrier: 1.5–3.0× daily vol
- Stop-loss barrier: 1.0–2.0× daily vol
- Time expiration: 5–21 business days
- More economically meaningful than fixed-horizon returns

---

## Backtesting (M3) — enhanced

| Library | When | Why |
|---------|------|-----|
| vectorbt | Research | Vectorised, fastest for parameter sweeps and walk-forward |
| backtrader | Strategy dev | Event-driven, realistic broker simulation |

**Mandatory validation requirements from research:**
- Purged k-fold CV with embargo period — never standard k-fold
- Deflated Sharpe Ratio test for multiple testing correction
- Probability of Backtest Overfitting (PBO) — >50% = likely overfitting
- Walk-forward validation with realistic costs
- Include delisted securities — survivorship bias is severe

**Realistic cost model (mandatory from day one):**
- Half-spread + commission + square-root market impact + borrowing costs
- Rule of thumb: 1–3 bps per trade for liquid equities, much higher for small-caps/altcoins

---

## Portfolio Optimisation (M4) — new from research report

| Library | Why |
|---------|-----|
| riskfolio-lib | Most comprehensive. HRP, HERC, CVaR, 24 risk measures, Black-Litterman, factor models. |
| pypfopt | Black-Litterman with Idzorek method. Minimum variance outperforms max-Sharpe out-of-sample. |
| cvxpy | Convex optimisation. CVaR minimisation via linear programming. Kelly criterion. |

**Key findings:**
- **HRP** — no matrix inversion, handles singular matrices, lower out-of-sample variance. Use as default over mean-variance.
- **CVaR** (Expected Shortfall) replaces VaR — Basel III standard, sub-additive, captures tail severity.
- **Half-Kelly** — Ed Thorp's recommendation. 75% of full Kelly growth, dramatically lower variance. Full Kelly too sensitive to estimation errors.
- **Volatility targeting** — scale positions inversely to realised volatility. EWMA λ=0.94. Improves risk-adjusted returns across all asset classes.
- **Ledoit-Wolf shrinkage** — optimal intensity ~80%. Reduces estimation error in covariance matrix.

---

## Execution (M5) — new from research report

| Library | Why |
|---------|-----|
| almgren-chriss | Optimal execution. Minimises E[Cost] + λ·Var[Cost]. Closed-form trajectory. |

**Square-root impact law — universal across all markets:**
```
Impact ≈ c × σ_daily × √(Q/V_daily)
```

| Asset | c value | Typical spread |
|-------|---------|---------------|
| Large-cap equities | 0.5–1.0 | ~1 bps |
| Small-cap equities | 1.0–2.0 | ~10+ bps |
| FX majors | ~0.2 | ~0.5 bps |
| BTC | ~1.0 | ~2 bps |
| Altcoins | 1.5–3.0 | ~20+ bps |

**Critical:** Naive vs optimal execution consumes 50-100% of gross alpha for medium-frequency strategies. Model this from day one.

**Funding rate arbitrage (crypto) — from research:**
- Long spot + short perpetual = collect positive funding, zero price risk
- Typical annualised returns: 10-15% normal markets, 100%+ during euphoria
- Monitor via `ccxt.fetch_funding_rate()`
- Key risks: margin depletion if funding reverses, execution slippage

---

## Deep Learning (M6) — from research report

| Library | Model | Use case |
|---------|-------|----------|
| darts | TCN | TCN outperforms LSTM on 10/11 benchmarks. Trains 3-10x faster. Use for price forecasting. |
| darts | TFT | Temporal Fusion Transformer — handles static covariates, interpretable. |
| transformers | FinBERT | `ProsusAI/finbert` — 5-20ms per sentence GPU. Standard for financial sentiment. |
| stable-baselines3 | PPO | RL for execution optimisation — not directional prediction (M8+). |

**When to use deep learning (evidence-based):**
- Limit order book prediction (DeepLOB) ✅
- Multi-scale time-series (TCN, TFT) ✅
- NLP sentiment extraction ✅
- Raw return prediction vs gradient boosting — gradient boosting wins for daily frequency ❌

---

## AI & NLP (M6) — Claude-first now, hybrid later

### Current production route (implemented in M6 pass):

| Task | Route | Model | Latency |
|------|-------|-------|---------|
| News headline classification | Claude API | `claude-sonnet-4-5` (configurable) | ~1-3s |
| Sentiment/event/direction scoring | Claude API | `claude-sonnet-4-5` (configurable) | ~1-3s |
| Macro regime labeling | Local deterministic logic over FRED data | n/a | <100ms |
| Trade rationale generation | Claude API | `claude-sonnet-4-5` (configurable) | ~1-3s |
| SEC/Reddit ingestion | Scaffold only | n/a | n/a |

### Persistence/audit additions (implemented):
- `storage.models.AIOutputLog` (`ai_outputs`) stores AI news, macro, and rationale outputs.
- Alembic migration `f27c0a1b9e10_add_ai_outputs_table.py` creates auditable AI output storage.
- `run_m3.py` and `run_m5.py` persist AI-linked metadata and signal-linked rationale rows.

### Planned evolution (kept from research):
- Move to hybrid local/API routing when throughput or cost profile justifies it.
- Use local model serving for sub-second high-volume classification if needed.

**Break-even note:** Local RTX 4090 can break even vs API in 6-8 weeks at >£400/month spend. Current choice remains pure Claude API for M6.

**Quantisation guide:**
- Q4_K_M: within ~3% perplexity — use for classification
- Q5_K_M or Q8: numerical reasoning
- Avoid Q3 and below

**LoRA fine-tuning:** rank r=16, alpha=32. QLoRA (4-bit NF4) fits 7B model on RTX 4090 at 8-10GB VRAM. 500 high-quality examples beats 5,000 noisy ones.

---

## Storage

| Technology | Why |
|-----------|-----|
| PostgreSQL | Primary database. ACID. Orders, fills, P&L, signals, audit log. |
| TimescaleDB | PostgreSQL extension. 10-100x faster for time-series queries. |
| Redis | In-memory. Live prices, active signals, Kalman filter state, GARCH estimates. |
| SQLAlchemy | ORM layer. Database-agnostic. |
| Alembic | Schema migrations versioned. |

---

## Parameter Orchestration (M5.2+)

| Component | Why |
|-----------|-----|
| `config/fundamentals.yaml` | Risk parameter defaults with absolute safety bounds and source rationale. |
| `risk/parameters.py` | Layered `ParameterManager`: regime override > AI recommendation > proven default. |
| `storage.models.ParameterLog` + Alembic | Durable audit trail for every parameter change, rejection, and expiry reversion. |

Policy summary:
- AI recommendations apply only above confidence threshold and within absolute bounds.
- Regime overrides have precedence over AI recommendations.
- Expired overrides auto-revert and are logged for auditability.

---

## Infrastructure

| Technology | Why |
|-----------|-----|
| Docker + Compose | Containerise all components. One command startup. |
| python-dotenv | Secrets management. Keys never in code. |
| Celery + Redis | Scheduled jobs — daily GARCH refitting, model retraining, rebalancing. |
| VPS (Hetzner/DigitalOcean) | €5-15/mo always-on. Low latency to exchange APIs. |

---

## API & Dashboard

| Technology | Why |
|-----------|-----|
| FastAPI | REST + WebSocket for dashboard. Async native. |
| React + Vite | M7 dashboard UI with control-plane actions and realtime status updates. |
| Grafana (optional) | Operational metrics — API latency, error rates. |

M7 control-plane implementation notes:
- Postgres-backed command/state bus (`control_commands`, `control_state`) for API-to-runner control actions across processes.
- FastAPI mutating endpoints support env-token auth (`API_CONTROL_TOKEN`) and configurable CORS origins (`API_ALLOWED_ORIGINS`).
- Optional read-path auth: `DASHBOARD_READ_TOKEN` (header `X-Dashboard-Token` or `Authorization: Bearer`) on HTTP routes; WebSocket `?token=`; optional `POST /auth/dashboard/login` with `DASHBOARD_PASSWORD` returning the read token.
- WebSocket pushes `tick` messages with `status` plus `events` (runner `dashboard.events` tail + new `signals` / filled `orders` rows).
- `GET /control/commands/{id}` for polling command status.
- `run_m5` `--control-poll-interval-sec` (default 5) applies `control_commands` frequently outside the main iteration.
- `ParameterManager` loads/persists regime overrides to `config/risk_parameter_overrides.yaml` (`RISK_PARAMETER_OVERRIDES_PATH`); mirrors effective values in `control_state` for API display.
- **M8 micro-live:** `config/m8_micro_live.yaml` merged into risk config; extra gates in `RiskEngine` when `enabled` and `APP_ENV=live` (symbol/strategy whitelist, max notional per order).

---

## What's NOT in the stack (and why)

| Technology | Why excluded |
|-----------|-------------|
| Kafka/RabbitMQ | Overkill. Redis queues sufficient. |
| Kubernetes | Too early. Docker Compose on VPS is right level. |
| TensorFlow | PyTorch is quant finance standard. |
| MongoDB | Financial data is relational. PostgreSQL correct. |
| Bloomberg Terminal | $24K/year. Polygon.io + FRED + Benzinga instead. |
| RavenPack | $50-200K/year. FinBERT + Claude API instead. |
| Full Kelly criterion | Too sensitive to estimation errors. Half-Kelly always. |
| Standard k-fold CV | Massively overstates performance. Purged CV only. |
| Float for prices | Rounding errors compound over thousands of trades. Decimal always. |
| SMOTE for imbalance | Violates temporal structure. Use cost-sensitive learning instead. |
