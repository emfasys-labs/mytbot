# IBKR connectivity notes

IBKR is not like Kraken, Binance, Bybit, or Alpaca. For ordinary individual/retail
use, there is no simple API-key connector that logs directly into IBKR's trading
backend.

## Supported paths

1. **TWS API / IB Gateway socket API**
   - What `brokers/ibkr/adapter.py` uses through `ib_insync`.
   - Requires an already authenticated TWS or IB Gateway process.
   - Paper defaults to port `7497`; live defaults to `7496`.
   - This is the richest path for orders, account data, market data, options,
     forex, and legacy IBKR workflows.

2. **Client Portal / Web API**
   - REST/WebSocket shape, but individual users still normally run a local Java
     Client Portal Gateway for authentication/session mediation.
   - It can reduce dependence on TWS API semantics, but it does not remove the
     local authenticated IBKR gateway problem for typical individual accounts.

3. **Direct OAuth Web API / FIX**
   - Direct hosted API/OAuth and FIX are real IBKR products, but they are
     onboarding/connectivity tracks rather than a normal retail API-key flow.
   - FIX order routing requires institutional/enterprise-style connectivity, or
     use through IB Gateway for non-institutional users.

## Current failure pattern: paper disclaimer blocks API clients

Gateway can show:

- `Interactive Brokers API Server: connected`
- `Market Data Farm: ON`
- `API Client: disconnected`

and still reject every third-party API socket with:

```text
Paper trading disclaimer must first be accepted for API connection.
```

In that state Gateway is logged in and may publish account values internally, but
`mytbot` cannot use it as a broker. The operator must accept the paper trading
disclaimer / API-client prompt in Gateway. If no prompt is visible, restart
Gateway and watch the UI during the first API connection attempt.

## Host/port mismatch

`.env` currently points the bot at:

```text
IBKR_HOST=127.0.0.1
IBKR_PORT=7497
```

If Gateway is listening only on another interface, the bot will report IBKR as
offline even when the Gateway status window looks healthy. Use:

```powershell
Get-NetTCPConnection -LocalPort 7497
```

and compare the listener with `IBKR_HOST`. `scripts/diagnose_ibkr_gateway.py`
also tries `127.0.0.1`, `localhost`, and `::1` read-only.

## Diagnostic command

From repo root with the Python environment active:

```powershell
python scripts/diagnose_ibkr_gateway.py
```

The script does not place orders. It attempts a read-only connection with
`IBKR_DIAG_CLIENT_ID` (default `31`) and classifies common failures:

- `gateway_blocked_by_paper_disclaimer`
- `duplicate_client_id`
- `port_refused_or_api_listener_not_accepting`
- `api_handshake_timeout`

## Recommended operating setup

- Keep `IBKR_CLIENT_ID=1` reserved for `mytbot`.
- Use `IBKR_DIAG_CLIENT_ID=31` for diagnostics.
- Enable API socket clients in Gateway/TWS and allow localhost/trusted IPs.
- Accept paper trading disclaimers immediately after Gateway login.
- Avoid logging into Client Portal with the same username while Gateway is
  expected to stay connected across resets.
- Treat IBKR as optional at runtime: if it is excluded from coverage, all
  dashboard and routing numbers must use the current connected broker set.
