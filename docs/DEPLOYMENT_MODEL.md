# myTbot — Deployment Model & Productization Plan

Status: living plan for **M11 — Productization**. Companion to
`docs/PRODUCT_TECHNICAL_SPEC.md` (what the platform does) and
`docs/BUILD_PLAN.md` (milestone tracking). myTbot is open source under AGPL v3,
published by Emfasys Labs. Not investment advice; not a managed service.

---

## 1. Positioning: one product, several ways to run it

**myTbot is a private, self-hosted personal trading system. Run it on your own
computer, a dedicated home machine, or your own cloud server. Your broker
accounts, API credentials and capital remain under your control.**

One codebase, one risk engine, one Connect Hub, one dashboard, one set of broker
adapters. What changes between deployments is only **where the always-on trading
engine is hosted**. "Self-hosted" means the user controls the environment — not
that it must be a local computer.

---

## 2. The three run modes

All three are self-hosted. The difference is purely *which machine runs the
always-on backend* and *where you view it from*.

| Mode | Where the engine runs | Where you view / control it |
|------|----------------------|------------------------------|
| **Run here** | Your everyday laptop/desktop | Same machine |
| **Run on my home server** | A separate always-on PC / mini-PC / home server | From your laptop, phone, or browser, remotely |
| **Run in my cloud account** | A Linux VM the user rents (Oracle, Hetzner, AWS, …) | Remotely from any device |

For **home server** and **cloud**, the split is:

```
Always-on host (home mini-PC or cloud VM)
├─ myTbot backend / trading engine
├─ Database
├─ Broker API connections + credentials
├─ Risk engine + execution
├─ IB Gateway (only if using IBKR — must be co-located)
└─ Optional local AI models

Your laptop / phone
└─ myTbot dashboard + controls (browser or desktop app)
```

**Architectural note — this split already works today.** The API binds
`0.0.0.0` with a configurable `API_HOST` (`run.py`), and the UI can target any
backend via `VITE_API_BASE` plus a control token it stores in `localStorage`
(`ui/src/app/lib/api.ts`). What's missing is the *guided UX* and the *secure
remote-access* layer, not the core capability.

**IBKR placement rule:** IB Gateway/TWS must run on the **same host as the
backend** (myTbot connects to it locally over its API port). Crypto venues
(Kraken, Binance, Bybit) and most REST brokers connect directly from wherever
the backend runs.

---

## 3. Does myTbot need to run 24/7?

Only for **autonomous** operation.

- **Research / dashboard / backtest / manually-approved trades:** no — start it
  when you want.
- **Autonomous monitoring + trading:** yes. While the host is offline myTbot
  cannot ingest data, evaluate strategies, monitor risk, reconcile positions,
  issue system-managed stop/reduce-only actions, or place new orders.

A **broker-native** stop order may remain active at the broker, but myTbot's own
monitoring and risk processes are inactive while the host is offline. This
asymmetry is the whole reason the home-server and cloud modes exist.

---

## 4. Three audiences (open source ≠ command line for everyone)

| Route | Who | Experience |
|-------|-----|------------|
| **Operator** | Most users | Download the desktop app → setup wizard → connect a broker → paper mode → dashboard. No terminal, YAML, or Docker. |
| **Self-hosted** | Technically comfortable | Docker deploy, own server, own backups/domain, edit config files. The normal advanced OSS path. |
| **Developer** | Contributors / researchers | GitHub: build adapters, add feeds, improve strategies, test models. |

GitHub is the source + community home; it must **not** be the only way an
ordinary user can install myTbot.

Even on the operator route, the product must never make live trading feel
consequence-free. Users must still understand: paper vs live, read-only vs
trading permissions, leverage/CFD risk, broker account types, API credentials,
and that automated trading can lose money.

---

## 5. The desktop app is a control plane, not a separate product

There is one product: **myTbot**. The desktop app is just the easiest way to
install, configure, and operate it. It provides: machine-suitability check,
install/updates, local service start/stop, Connect Hub, broker/API setup,
paper-mode onboarding, dashboard, secure remote connection to a user-owned
cloud/home instance, and backup/migration tools. The engine runs locally or
remotely; the desktop app connects to whichever instance the user chose.

---

## 6. Installation profiles

Don't force every user to download the heaviest AI stack. The setup wizard
inspects the machine (`connectors/machine_probe.py` already exists) and
recommends a profile; Local-AI models download **only** if chosen.

| Profile | Includes | For |
|---------|----------|-----|
| **Lite** | Core engine, rules, dashboard, broker connections. **SQLite, no Docker.** | Paper trading, lower-spec machines, easiest install |
| **Standard** | Core + sentiment + broader data features. Docker stack (Postgres/Timescale). | Most users with a capable PC |
| **Local AI** | Full local reasoning models + enhanced AI. | Substantial RAM/disk, ideally a GPU |

---

## 7. Connect Hub stays curated

Users must not wire arbitrary unknown broker APIs and trade through them.
Connect Hub presents a **tested catalogue** and, per connector, shows: credentials
configured, connection test, balances readable, market data, paper available,
live permitted, **Certified vs Experimental**, and the next required action.
IBKR is labelled **Advanced setup** (needs IB Gateway/TWS + API port). Each
connector exposes its correct connection method (API key / OAuth / local gateway
/ terminal bridge). New connectors begin **Experimental** until verified
(`connectors/certification.py` — the risk engine fail-closes execution to
certified only).

**MVP:** a new user completes onboarding with **one broker, paper mode, no
treasury, free data, no paid AI** (`connectors/onboarding.py` already enforces
"≥1 broker + paper = launchable").

---

## 8. Migration & duplicate-trading safety

A user must never accidentally leave two myTbot instances trading the same broker
account. For v1, migration requires: stop old instance → confirm disarmed →
move/rotate broker credentials → deploy new → read-only connection + balance
checks → re-arm paper first → explicit live confirmation only after checks pass.

A truly enforced cross-device lock needs a shared coordination point. Until that
exists, the safe approach is **credential rotation + a single-armed-instance
execution lock** (a control-state token only one instance can hold, plus a
heartbeat-based stale-instance detector) and a formal migration checklist. Do
not pretend a local warning alone guarantees two installs never trade at once.

---

## 9. Current state vs gaps (grounded in the repo)

### Already built
- Connect Hub: catalogue, capability probe, certification, lifecycle, machine
  probe, onboarding wizard (`connectors/`), API `/connect/*`, `docs/CONNECT_HUB.md`.
- Remote split: `API_HOST=0.0.0.0`, `VITE_API_BASE`, control-token banner, CORS.
- Docker deploy + **IBKR Gateway in Docker** (`docker/ibkr-gateway`, `docs/IBKR_GATEWAY_DOCKER.md`).
- Paper-first + micro-live guardrails (`docs/M8_MICRO_LIVE.md`).
- OSS governance (AGPL, CONTRIBUTING, SECURITY, DISCLAIMER, COMMERCIAL).
- Local-AI catalogue (`config/local_llm_catalogue.yaml`).

### Gaps
1. Install profiles don't yet gate *what gets installed* (probe exists; wiring doesn't).
2. **No desktop app / installer** — biggest net-new build.
3. No guided run-mode selection or packaged always-on background service.
4. No secure-remote-access story (shared token on `0.0.0.0` is LAN-only, not internet-safe).
5. No BYO-cloud SSH deploy.
6. No enforced single-armed-instance migration lock.
7. Responsive dashboard exists in part (`ui/src/app/mobile.tsx`) but isn't the real responsive layout.

---

## 10. Lite / SQLite feasibility — **HIGH** (spike proven)

The storage layer is far more portable than expected:

- Schema is built via `Base.metadata.create_all` at startup (`storage/db.py`),
  **not** Alembic — Lite can create the whole schema on SQLite and skip migrations.
- Column types are all generic (`Numeric`, generic `JSON`, `DateTime`) — no
  `JSONB`/`ARRAY`/`UUID`.
- **Redis is already optional** — no app code uses a client; `dependency_manager`
  logs *"system will run without cache"*.
- TimescaleDB hypertable creation is already best-effort and degrades on plain
  backends.

**Work required (contained):** a `DATABASE_URL`/`DB_BACKEND=sqlite` branch in
`storage/db.py` + `alembic/env.py`; rewrite **2 runtime `DISTINCT ON` queries**
(`api/server.py`, `data/regime_metrics.py`) to window-function form; skip the
Postgres/Redis bring-up in `dependency_manager` for Lite.

### The one real gotcha — Decimal precision (fix proven)
Project rule #3 is "Decimal, never float." On SQLite, SQLAlchemy's `Numeric`
falls back to `REAL` (float) affinity and **corrupts** money values. Proven by
`scripts/spike_sqlite_decimal.py`:

```
input                      Numeric(REAL)              DecimalText(TEXT)
12345678901.234567890123   12345678901.234567642212   12345678901.234567890123
9999999.99999999           9999999.999999990687       9999999.99999999
70123.45                   70123.449999999997         70123.45
plain Numeric round-trips exactly: False   <- the gotcha
DecimalText round-trips exactly:   True    <- the fix
```

**Fix:** a TEXT-backed `DecimalText` `TypeDecorator` storing the canonical
Decimal string. Replace bare `Numeric` on price/qty columns with it (or a shared
custom type) and add a precision test. This is the only part of Lite that needs
care — everything else is mechanical.

---

## 11. Connect Hub UI audit — ~80% built; gaps are presentation only

The UI already renders a per-connector card (`ui/src/app/redesign/screens.tsx`):
status pill, capabilities, missing-secrets, next action; the backend already
carries `last_test_result`, `detected_capabilities`, `certification`, and
`machine_probe`. The remaining spec gaps were **UI-only** and are now addressed
(M11 Phase 1):

- ✅ **Certification badge** (Certified / Experimental) on broker & treasury cards.
- ✅ **"Advanced setup"** badge for gateway/bridge auth types (IBKR), with a
  tooltip explaining IB Gateway/TWS + API port.
- ✅ **Explicit status checklist** for brokers (connection test / balances / paper
  / live) plus asset-coverage pills, replacing the compressed capability list.

Remaining (optional): richer per-`auth_type` setup guidance copy in the configure
wizard.

---

## 12. Phased implementation plan

See `docs/BUILD_PLAN.md → M11` for the live checklist. Summary:

- **Phase 0 — Decisions:** Lite = SQLite (no Docker); desktop = Tauri; remote =
  tunnel (Cloudflare/Tailscale); responsive PWA, no native mobile app.
- **Phase 1 — Operator edition (ship first):** Lite SQLite backend + `DecimalText`
  + portable queries; install profiles; Connect Hub UI polish (done); quick-start
  + backup docs.
- **Phase 2 — Desktop control plane:** Tauri shell, machine-suitability screen,
  run-mode selector, managed background service, signed installer + updates.
- **Phase 3 — Bring your own server:** SSH deploy, tunnel-based secure dashboard,
  migration lock + wizard.
- **Phase 4 — Wider catalogue:** new venues on real demand, Experimental-first.

---

## 13. Strategic balance

> Make myTbot easy to install locally, powerful enough to run continuously on
> user-owned infrastructure, and disciplined enough that convenience never
> weakens safety.

Default: **Download myTbot. Connect one broker. Run paper mode.**
Advanced: **Deploy on your own machine or cloud server; keep full control of your
infrastructure, credentials, and capital.** One product, one name, one promise.
