# IBKR Gateway resilience — runbook

## Why

IBKR Gateway is not merely flaky — it is *engineered* to drop: a
**mandatory daily restart** (built in since Gateway v974) plus a
**weekly re-authentication**. Hand-launching Gateway on the desktop
means those drops are unplanned multi-hour outages (we observed ~2/night,
10–27 min recovery, 53 error-spam lines).

The fix is two complementary layers — both now in place:

- **#2 (code, shipped):** the bot now detects IBKR down on every order
  resolution, refuses to route to a dead socket, defers reduce-only
  closes rather than booking a stale price, recovers ≈10 s after Gateway
  is genuinely back (probe-driven, was 10–27 min), and goes silent during
  a configured maintenance window instead of error-spamming.
- **#1 (infra, this doc):** run Gateway inside Docker with **IBC**, which
  auto-logs-in and performs the unavoidable daily restart as a controlled
  ~1-minute blip at a time *you* choose. mytbot points at the container.

## #1 — stand up the containerised Gateway

```sh
cd docker/ibkr-gateway
cp .env.example .env
# edit .env: TWS_USERID / TWS_PASSWORD (paper login), AUTO_RESTART_TIME
docker compose up -d
docker compose logs -f ib-gateway   # watch first login; 2FA via VNC :5900 if prompted
```

First run may need a one-time 2FA approval — connect a VNC viewer to
`127.0.0.1:5900` and approve on your phone. IBC handles logins after that.

### Point mytbot at it

In the **repo root `.env`**:

```
IBKR_HOST=127.0.0.1
IBKR_PORT=4002                       # 4002 = container PAPER api
IBKR_MAINTENANCE_WINDOWS=02:50-03:05 # 24h, cover AUTO_RESTART_TIME ± a few min
```

`IBKR_HOST` / `IBKR_PORT` are already env-driven — **no code change**.
Then restart mytbot under the supervisor:

```sh
python scripts/supervise.py
```

### Verifying

- `docker compose ps` → `mytbot-ib-gateway` is `healthy`.
- mytbot `/system/status` → `brokers.ibkr.connected: true`.
- During the maintenance window: mytbot log shows IBKR cleanly skipped
  (no `Socket disconnect` error burst); `brokers.ibkr` goes unavailable
  then auto-recovers within ~1 min of the window ending.

## Going live (later — deliberate)

When you switch to real money:

1. In `docker/ibkr-gateway/.env`: `TRADING_MODE=live`, swap to the live
   IB login, `docker compose up -d`.
2. In repo `.env`: `IBKR_PORT=4001` (container LIVE api) and set
   `APP_ENV=live`.
3. **Caveat:** `brokers/ibkr/adapter.py` currently hardcodes the live
   socket port to `7496` (`self.port = port if paper_mode else 7496`).
   Pointing live at the container's `4001` needs that line to honour the
   `IBKR_PORT` override — this is deliberately deferred to the **B4
   live-arming interlock** work (a single env var should not silently
   route real money). Do not flip to live before B4 is addressed.

## Tuning

| Setting | Where | Purpose |
|---|---|---|
| `AUTO_RESTART_TIME` | `docker/ibkr-gateway/.env` | When IBC restarts Gateway (controlled blip) |
| `IBKR_MAINTENANCE_WINDOWS` | repo `.env` | When mytbot proactively treats IBKR as down (no attempts/spam). Format: `HH:MM-HH:MM`, comma-separated, optional `Ddd ` weekday prefix, may wrap midnight |
| `IBKR_HOST` / `IBKR_PORT` | repo `.env` | Where the adapter connects (the container) |

Keep `IBKR_MAINTENANCE_WINDOWS` a little wider than the Gateway restart
so the bot is silent for the whole blip and resumes automatically after.
