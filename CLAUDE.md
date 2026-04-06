# CLAUDE.md
# ==========
# This file is for Claude (claude.ai).
# Paste the contents of this file at the start of any Claude conversation
# to instantly bring Claude up to speed on the project state.
#
# Update the CURRENT STATE section after each work session.

## PROJECT
`mytbot` — personal autonomous multi-asset trading system.
GitHub: https://github.com/kvcom/mytbot.git
Owner: UK-based, trading stocks, bonds, ETFs, forex, crypto.
Primary broker: IBKR Pro. Crypto: Kraken + Binance.

## ARCHITECTURE IN ONE PARAGRAPH
All brokers implement a single abstract interface in `brokers/base.py`.
The signal engine aggregates strategy outputs into a Signal.
Every Signal passes through the risk engine (unconditional veto power).
Approved signals go to the execution engine which routes to the best broker.
Everything is logged. Runtime broker permissions support graceful fallback routing.
Risk parameters are managed by `ParameterManager` with layered overrides
(regime > AI > defaults) and bounded, auditable changes.
AI (Claude API) classifies news and generates rationale — never places orders.

## KEY FILES
- `brokers/base.py`          — the adapter interface (FROZEN, never change)
- `brokers/registry.py`      — add new brokers here (one line)
- `brokers/_template/`       — copy this to add any new exchange
- `risk/engine.py`           — risk checks, kill switch
- `risk/parameters.py`       — parameter manager (defaults + overrides + expiry)
- `signals/engine.py`        — signal aggregation
- `strategies/momentum.py`   — first strategy (momentum breakout)
- `execution/engine.py`      — order placement
- `execution/router.py`      — smart order routing
- `storage/models.py`        — database schema
- `config/risk_limits.yaml`  — all risk thresholds (editable without code change)
- `config/fundamentals.yaml` — parameter defaults, absolute bounds, AI policy
- `config/broker_permissions.yaml` — runtime broker permission/fallback map
- `config/data_pipeline.yaml` — M2 symbols, intervals, News/FRED toggles
- `run_pipeline.py`          — M2: yfinance → features → Postgres; NewsAPI + FRED
- `docs/DECISIONS.md`        — architectural decision log
- `docs/M2_READINESS.md`     — M1 verification summary + M2 prep checklist
- `alembic/`                 — DB migrations (URL from POSTGRES_* in env.py)
- `tests/`                   — pytest smoke tests
- `.cursorrules`             — Cursor AI alignment rules

## CURRENT STATE
<!-- Update this section after each work session -->
- Milestone: M5 — Execution engine + full pipeline ✅ (deliverable: autonomous paper loop + full execution safety rails)
- Last completed task: Added autonomous parameter-management foundation (`config/fundamentals.yaml`, `risk/parameters.py`, `parameter_log` table + migration), plus capital-tier allocator support
- Last completed task: Added autonomous parameter-management foundation (`config/fundamentals.yaml`, `risk/parameters.py`, `parameter_log` table + migration), plus proportionality-based asset gating (no capital tiers)
- Next task: M6 — AI intelligence layer (news classification, rationale generation, regime-aware signal modifiers)
- Blockers: IBKR stream/order need local IB Gateway/TWS; Kraken/Binance live connectivity needs API keys; 2+ week paper soak is an operational runtime validation step
- Notes: .env not committed — use .env.example; set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` for critical alerts

## RULES CLAUDE MUST FOLLOW IN THIS PROJECT
1. Never change `brokers/base.py` interface — it is frozen
2. Never add a bypass to the risk engine
3. Decimal for all prices and quantities, never float
4. paper_mode=True is always the default
5. Every new broker = one new file + one line in registry.py, nothing else
6. Log every signal, risk decision, order, and fill
7. AI (Claude API) only scores and explains — never executes
8. Check `docs/DECISIONS.md` before making architectural choices

## HOW TO USE THIS FILE
At the start of a Claude session, say:
"Here is my project context: [paste this file]
Current task: [describe what you want to work on]"

Claude will immediately understand the full architecture and constraints
without needing to re-explain everything.
