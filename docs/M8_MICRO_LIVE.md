# M8 — Micro-live rollout

This milestone moves from paper-only to **small real-capital** trading with tight guardrails.

## What is implemented in code

- **`config/m8_micro_live.yaml`** — optional profile: symbol list, strategy list, max notional per order, **`strategy_sleeve_caps`** (per-strategy max order as % of portfolio).
- **Risk engine** — when `enabled: true` **and** `APP_ENV=live`, signals must pass:
  - `m8_symbol_whitelist`
  - `m8_strategy_whitelist`
  - `m8_max_notional` (USD and/or GBP-based cap via `M8_GBP_USD_RATE`)
  - **`m8_strategy_sleeve_cap`** when `strategy_sleeve_caps` is set
- **Runners** (`run_m3`, `run_m5`) load the profile via `--m8-config` (default: `config/m8_micro_live.yaml`).
- **Second crypto venue** — **Bybit** (`brokers/bybit/adapter.py`): set `BYBIT_API_KEY` / `BYBIT_API_SECRET`, optional `BYBIT_TESTNET`, `BYBIT_CATEGORY=linear|spot`. `run_m5` default `--available-brokers` includes `bybit`. Router prefers **Bybit** for `asset_class=future` (USDT perps).
- **Binance live** — same as before: `BINANCE_*` keys, `BINANCE_TESTNET` / `BINANCE_PAPER_MODE`; adapter already supports live API when `paper_mode=False`.
- **Volatility-sized signals** — `config/strategies.yaml` → `signal_engine.volatility_sizing` scales quantity from strategy **`atr_pct`** metadata (momentum + mean reversion).

Paper / `APP_ENV=paper` runs ignore M8 gates so development stays unchanged.

## Operational checklist before enabling

1. **IBKR live** — TWS or IB Gateway on **port 7496** (live), API enabled, correct account id in `.env`.
2. **`APP_ENV=live`** in `.env` (and understand that downstream configs use this).
3. **`run_m5 --live`** when you intend real broker orders (execution engine `paper_mode` follows this flag).
4. **Set `enabled: true`** in `config/m8_micro_live.yaml` only when the checklist above is satisfied.
5. **Align symbols** — `data_pipeline.yaml` / `--symbols` should match `symbol_whitelist` (e.g. single symbol SPY only).
6. **Capital** — keep `max_notional_usd_per_order` tiny; align `portfolio-value` / account size with reality.
7. **Dashboard** — kill switch and `API_CONTROL_TOKEN` / `DASHBOARD_*` tokens set for production-style access.
8. **Alerts** — `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` for execution failures.
9. **Bybit (optional)** — API keys scoped to trade only; `BYBIT_CATEGORY` matches how you route (`linear` for perps / `future` asset class in strategies).

## GBP vs USD notional

US equity notionals are evaluated in **USD**. For a rough **£100** cap, use `max_notional_usd_per_order: ~125` or set `max_notional_gbp_per_order: 100` and `M8_GBP_USD_RATE` (USD per one GBP) in `.env`.

## What stays manual / iterative

- Weekly review cadence and metrics: see **`docs/M8_WEEKLY_REVIEW.md`**.
- Adding a second exchange adapter (Binance/Bybit) or volatility-only sizing changes are **follow-up work**, not required to call M8 “started.”
