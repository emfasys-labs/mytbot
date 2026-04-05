# TECH_STACK.md
# ==============
# Every technology choice with justification.
# Before adding a new library, check here first.
# If something isn't listed, it probably shouldn't be added yet.

---

## Language & Runtime

| Technology | Version | Why |
|-----------|---------|-----|
| Python | 3.12+ | Best quant/finance ecosystem. pandas, numpy, TA-Lib all native. Async support for WebSocket streams. |
| asyncio | stdlib | All broker API calls are async. Concurrent WebSocket streams without threading complexity. |
| aiohttp | 3.9+ | Async HTTP client for REST API calls to brokers and news feeds. |

---

## Broker SDKs

| Library | Broker | Why |
|---------|--------|-----|
| ib_insync | IBKR | Best IBKR Python library. Wraps TWS API cleanly with async support. Widely used in quant community. |
| python-kraken-sdk | Kraken | Official Kraken Python SDK. REST + WebSocket. Maintained by Kraken team. |
| python-binance | Binance | Most mature Binance library. Handles rate limits, reconnection automatically. |
| alpaca-py | Alpaca | Official Alpaca SDK. Clean async API, paper/live toggle built in. |

**Adding a new broker SDK:** install it, import it inside the adapter file only.
Never import a broker SDK outside its adapter folder.

---

## Data & Feature Engineering

| Library | Why |
|---------|-----|
| pandas | Core data manipulation. OHLCV processing, feature engineering, rolling windows. |
| numpy | Numerical operations. Used internally by pandas and TA-Lib. |
| pandas-ta | Technical indicators — RSI, MACD, ATR, Bollinger Bands, momentum, 150+ indicators. |
| yfinance | Historical price data for backtesting and model training. Free, reliable for research. |
| Polygon.io (API) | Professional-grade market data. Used when yfinance isn't sufficient for production. |
| FRED API | Free Federal Reserve macro data — interest rates, CPI, employment. Essential for bonds/forex signals. |
| NewsAPI | Real-time news ingestion. Reuters, Bloomberg headlines, earnings calendars. |

---

## Strategy & Machine Learning

| Library | When to use | Why |
|---------|------------|-----|
| scikit-learn | Classical ML signals | Random Forest, SVM, regression for signal scoring. Deterministic and auditable. |
| XGBoost | Return prediction | Best performing on tabular financial data. Faster than neural nets for this use case. |
| LightGBM | Alternative to XGBoost | Slightly faster training. Use when XGBoost is too slow. |
| PyTorch | Time-series patterns | LSTM/transformer for sequence modelling. Only introduce when classical ML hits ceiling. |
| vectorbt | Backtesting | Fast, vectorised. Handles realistic fees, slippage, and position sizing. |

**Important:** Don't introduce PyTorch until scikit-learn/XGBoost strategies are proven.
Complexity is the enemy early on.

---

## AI Intelligence Layer

| Library | Why |
|---------|-----|
| anthropic | Official Claude API SDK. Used for news classification, event scoring, trade rationale. |

**Claude API is used for:**
- Classifying news headlines by event type and affected assets
- Scoring directional sentiment (-1.0 to +1.0)
- Generating plain-English trade rationale for audit log
- Detecting unusual narrative patterns

**Claude API is NOT used for:**
- Placing orders
- Portfolio allocation decisions
- Overriding risk engine

---

## Storage

| Technology | Why |
|-----------|-----|
| PostgreSQL | Primary database. ACID compliant — critical for financial data. Orders, fills, P&L, audit log. |
| TimescaleDB | PostgreSQL extension for time-series data. 10-100x faster for OHLCV queries vs plain PostgreSQL. |
| Redis | In-memory cache for live prices, active signals, broker state. Sub-millisecond reads. |
| SQLAlchemy | ORM layer. Database-agnostic models — swap PostgreSQL later without rewriting queries. |
| Alembic | Database migrations. Schema changes tracked and versioned. |

---

## Infrastructure

| Technology | Why |
|-----------|-----|
| Docker | Containerise every component. Reproducible across machines. |
| Docker Compose | Orchestrate all containers (bot, api, db, redis) with one command. |
| python-dotenv | Secure secrets management. API keys in .env files, never in code. |
| Celery + Redis | Task queue for scheduled jobs — daily rebalancing, model retraining, reports. |
| VPS (Hetzner/DigitalOcean) | €5–15/mo. Always-on server. Low latency to exchange APIs. |

---

## API & Dashboard

| Technology | Why |
|-----------|-----|
| FastAPI | REST API backend for dashboard. WebSocket support for real-time updates. Fast, async native. |
| uvicorn | ASGI server for FastAPI. Production-grade. |
| React | Dashboard frontend. Component-based, good charting ecosystem. |
| Recharts | P&L charts, position tables, signal logs. Composable, React-native. |
| Grafana (optional) | Operational metrics — API latency, error rates, system health. Plugs into PostgreSQL. |

---

## Monitoring & Logging

| Library | Why |
|---------|-----|
| Loguru | Structured Python logging. Replaces stdlib logging. Every decision searchable and queryable. |
| httpx | HTTP client for alert webhooks (Telegram, email). |

---

## Development Tools

| Tool | Why |
|------|-----|
| pytest | Testing framework. |
| pytest-asyncio | Async test support — needed for broker adapter tests. |
| Black | Code formatter. 100 char line length. |
| Cursor | Primary IDE with AI assistance. Reads `.cursorrules` for project alignment. |

---

## What's NOT in the stack (and why)

| Technology | Why excluded |
|-----------|-------------|
| Celery (initially) | Overkill for M1-M5. Simple asyncio loops are enough early on. Add in M6+. |
| Kafka/RabbitMQ | Overkill. Redis queues are sufficient for this scale. |
| Kubernetes | Way too early. Docker Compose on a VPS is the right level. |
| TensorFlow | PyTorch is the standard in quant finance. No reason to use both. |
| MongoDB | Financial data is relational. PostgreSQL is the right choice. |
| WebSockets (custom) | Each broker SDK handles WebSocket internally. Don't reinvent. |
| HFT libraries | Not targeting millisecond execution. Retail algo speed is fine. |
