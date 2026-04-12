# BROKERS.md
# ===========
# Everything about each broker — fees, setup, API keys, limitations.
# Reference this before implementing any broker adapter.

---

## Broker Priority

| Priority | Broker | Role | Account status |
|----------|--------|------|---------------|
| 1 | **IBKR Pro** | Primary — stocks, bonds, ETFs, forex, crypto (11 coins) | ⏳ Open now |
| 2 | **Kraken** | Crypto primary — 640+ pairs, GBP-native | ✅ Have account |
| 3 | **Binance** | Crypto secondary — highest liquidity | ✅ Have account |
| 4 | **Alpaca** | US equities paper trading | ✅ Adapter + `test_alpaca.py` |
| 5 | **Bybit** | Crypto spot + USDT linear perps | ✅ Adapter (`brokers/bybit/`, `pybit`) |
| 6 | **Deribit** | Crypto options only | ⏳ Much later |

---

## IBKR — Interactive Brokers Pro

**What it trades:** US equities, UK equities, EU equities, ETFs, bonds, forex, options, futures, 11 crypto assets (BTC, ETH, SOL, ADA, XRP, DOGE, LTC, BCH, LINK, AVAX, SUI)

**Fees:**
- US equities: ~$0.005/share (tiered by volume)
- Crypto: 0.12%–0.18% (no hidden fees, no spread markup)
- Bonds/ETFs: varies by product
- No inactivity fee if generating $10+/month in commissions

**Account type needed:** IBKR Pro (not Lite)
- Lite has no API access to international markets
- Pro has full TWS API, all global markets

**Permissions to request when opening:**
- Stocks (US + international)
- Bonds
- Options
- Futures
- Forex
- Crypto assets

**API setup:**
1. Install TWS (Trader Workstation) or IB Gateway on your machine
2. Enable API: TWS → Edit → Global Configuration → API → Settings
3. Check "Enable ActiveX and Socket Clients"
4. Paper trading port: **7497**
5. Live trading port: **7496**
6. Keep "Allow connections from localhost only" enabled (recommended)

**Python SDK:** `ib_insync`
**Docs:** https://interactivebrokers.github.io/tws-api/

**ENV vars needed:**
```
IBKR_HOST=127.0.0.1
IBKR_PORT=7497
IBKR_CLIENT_ID=1
IBKR_ACCOUNT_ID=DU1234567   ← your paper account ID
```

**Smoke tests:**
- Paper: `.\test-ibkr.ps1`
- Live: `.\test-ibkr.ps1 -Live`

`test_ibkr.py` now respects `APP_ENV` (`paper`/`live`) and supports explicit flags `--paper` / `--live`.

**Limitations:**
- TWS must be running on your machine (or IB Gateway on server)
- Paper and live are separate accounts with separate IDs
- Some products require additional permissions/approval
- Crypto only 11 coins (use Kraken/Binance for wider crypto universe)

---

## Kraken

**What it trades:** Crypto spot (640+ pairs), crypto futures, GBP pairs

**Fees (Kraken Pro — API trading tier):**
- Base: 0.25% maker / 0.40% taker
- Reduces with 30-day volume
- 0.00% maker / 0.05% taker at highest tier ($10M+ monthly)
- Use limit orders (maker) to pay 0.25% instead of 0.40%

**Note:** Standard Kraken app charges 1% — always use Kraken Pro for API trading

**Account:** Your existing account works. No special account type needed.

**API setup:**
1. Kraken → Security → API → Generate Key
2. Permissions needed: Query Funds, Query Orders & Trades, Create & Modify Orders
3. **Do NOT enable:** Withdraw Funds

**Python SDK:** `python-kraken-sdk`
**Docs:** https://docs.kraken.com/api/

**ENV vars needed:**
```
KRAKEN_API_KEY=
KRAKEN_API_SECRET=
# optional
KRAKEN_PAPER_MODE=true          # false allows real orders via adapter.place_order
KRAKEN_TEST_PLACE_ORDER=0       # 1 + paper false runs a tiny live market order in test_kraken.py
```

**Smoke test:** `python test_kraken.py` or `.\test-kraken.ps1` (uses `.env`).

**Paper trading:** No native paper mode. Adapter `paper_mode=True` skips sending orders but still uses the live API for balances and market data when keys are set.

**Limitations:**
- No native paper trading environment
- Rate limits: 15 requests/minute for private endpoints (increases with tier)
- Some pairs have minimum order sizes

---

## Binance

**What it trades:** Crypto spot (1000+ pairs), futures, options

**Fees:**
- Base: 0.10% maker / 0.10% taker (same for both)
- 25% discount if you hold BNB token and pay fees in BNB
- Effective rate with BNB: 0.075%

**Account:** Your existing account works. No special account type needed.

**API setup:**
1. Profile icon → API Management → Create API
2. Permissions needed: Read Info, Enable Spot & Margin Trading
3. **Do NOT enable:** Enable Withdrawals
4. Restrict to your IP address for security

**Python SDK:** `python-binance`
**Docs:** https://python-binance.readthedocs.io/

**ENV vars needed:**
```
BINANCE_API_KEY=
BINANCE_API_SECRET=
# optional
BINANCE_PAPER_MODE=true
BINANCE_TESTNET=false
BINANCE_TLD=com
BINANCE_TEST_PLACE_ORDER=0
```

**Smoke test:** `python test_binance.py` or `.\test-binance.ps1`

**Paper trading:** No separate “paper” on the main site. Options: (1) adapter `paper_mode=True` skips orders; (2) **Spot testnet** https://testnet.binance.vision/ with `BINANCE_TESTNET=true` and testnet keys.

**Limitations:**
- UK regulatory status has been patchy — monitor
- Rate limits: weight-based (1200 weight/minute)
- Some coins not available to UK users

---

## Alpaca

**What it trades:** US equities, ETFs, crypto (limited)

**Fees:**
- US stocks/ETFs: **zero commission**
- Crypto: ~0.15%–0.25%

**Account:** New account needed (not your existing Kraken/Binance).
- Go to: https://alpaca.markets
- Sign up with email
- UK residents supported — fund via Rapyd (GBP → USD conversion)
- Paper trading: instant, no funding needed

**API setup:**
1. Dashboard → Paper Trading → Your API Keys
2. Paper and live have **separate** API keys
3. Always start with paper keys

**Python SDK:** `alpaca-py`
**Docs:** https://docs.alpaca.markets/

**ENV vars needed:**
```
ALPACA_API_KEY=        ← paper key during M1
ALPACA_API_SECRET=     ← paper key during M1
ALPACA_PAPER_MODE=true
# optional: ALPACA_TEST_PLACE_ORDER=1  ALPACA_TEST_SYMBOL=AAPL
```

**Smoke test:** `python test_alpaca.py` or `.\test-alpaca.ps1`

**Paper trading:** Best paper trading environment of any broker.
Free, real-time data, up to $1M simulated capital.

**Limitations:**
- US market hours only: 9:30am–4:00pm ET (2:30pm–9:00pm UK)
- No UK stocks, no bonds, no forex
- Not FCA regulated (SEC/FINRA regulated in US — fine for personal use)
- Funding from UK requires GBP→USD conversion via Rapyd

---

## Bybit

**What it trades:** Crypto **spot** and **USDT-margined linear perpetuals** (V5 API). Options on Bybit are out of scope for this adapter unless extended later.

**Fees (typical):**
- Perpetual futures: ~0.02% maker / ~0.06% taker (check Bybit fee schedule)
- Spot: ~0.10% maker / ~0.10% taker

**Python SDK:** `pybit` (REST + WebSocket)

**ENV vars:**
```
BYBIT_API_KEY=
BYBIT_API_SECRET=
# optional
BYBIT_TESTNET=false
BYBIT_CATEGORY=linear    # linear | spot — matches routing for `asset_class=future` vs spot
```

**Implementation:** `brokers/bybit/adapter.py`, registered in `brokers/registry.py`. Router prefers Bybit for `asset_class=future` when configured (`execution/router.py`, `config/broker_permissions.yaml`).

**Paper / safety:** Adapter respects `paper_mode`; use testnet keys with `BYBIT_TESTNET=true` for dry runs.

---

## Deribit (Future — much later)

**What it trades:** Crypto options (BTC, ETH) — 85% of global crypto options market

**Fees:** 0.03% maker / 0.05% taker

**When to add:** Only if implementing options strategies (hedging, income generation).
This is advanced. Don't add until M8 is stable.

---

## Adding a New Broker — Checklist

When you decide to add any new exchange:

1. Check `docs/DECISIONS.md` — has this been decided already?
2. Copy `brokers/_template/adapter.py` to `brokers/newname/adapter.py`
3. Install the SDK: add to `requirements.txt`
4. Implement all 6 abstract methods (connect, disconnect, is_connected, get_balance, get_positions, place_order, cancel_order, get_order, get_open_orders, get_candles, get_order_book, get_last_price, stream_prices, get_supported_symbols, get_asset_class)
5. Add to `brokers/registry.py`: `"newname": NewNameAdapter`
6. Add to `execution/router.py` BROKER_ASSET_MAP
7. Add ENV vars to `.env.example`
8. Write unit tests in `tests/brokers/test_newname.py`
9. Add to `docs/BROKERS.md`
10. Add to `docs/DECISIONS.md`

**Nothing else in the system needs to change.**
