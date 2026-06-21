# myTbot — Quick Start

The fastest path to a running, paper-trading myTbot. For the full deployment
model (home server, cloud, run modes) see `docs/DEPLOYMENT_MODEL.md`; for a
deep, machine-by-machine reinstall see `docs/NEW_MACHINE_SETUP.md`.

> **myTbot trades real money when you connect a live broker and arm it. It
> starts in paper mode. Automated trading can lose money. You are responsible
> for your own credentials, capital, and risk.**

---

## Pick a profile

| Profile | Install | Needs Docker? | AI |
|---------|---------|---------------|-----|
| **Lite** | `pip install -r requirements-lite.txt` | **No** (SQLite) | Rules only |
| **Standard** | `pip install -r requirements.txt` | Yes (Postgres/Redis) | + FinBERT sentiment |
| **Local AI** | Standard + install [Ollama](https://ollama.ai) and pull a model | Yes | + local reasoning LLM |

Not sure? Start with **Lite**. The onboarding wizard also probes your machine
and recommends a profile (`install_profile` in the Connect Hub onboarding view).

---

## Lite quick start (no Docker — recommended first run)

```bash
# 1. Get the code + a Python 3.12+ virtual environment
git clone https://github.com/emfasys-labs/mytbot.git
cd mytbot
python -m venv .venv
# Windows: .\.venv\Scripts\Activate.ps1   |   macOS/Linux: source .venv/bin/activate
pip install -r requirements-lite.txt

# 2. Minimal config — copy the example and switch to the Lite (SQLite) backend
cp .env.example .env
#   in .env set:
#     DB_BACKEND=sqlite
#     APP_ENV=paper           (default — keeps every broker in paper mode)

# 3. Start everything (one command)
DB_BACKEND=sqlite python run.py        # Windows PowerShell: $env:DB_BACKEND='sqlite'; python run.py
```

`run.py` brings up the trading engine, API, and dashboard. With `DB_BACKEND=sqlite`
it **skips Postgres, Redis, and Docker entirely** and stores everything in
`data/mytbot.db`.

Then:

1. Open the dashboard (the URL is printed on startup, default `http://127.0.0.1:8000`).
2. Go to **Connect** → add **one** broker (e.g. Alpaca paper, Kraken, Binance).
   Paste its API key/secret. IBKR is labelled **Advanced setup** — it needs IB
   Gateway/TWS running locally (see `docs/IBKR_CONNECTIVITY.md`); skip it for now.
3. Confirm the broker card shows **connection test ✓** and **paper ✓**.
4. Press **ON**. You are now paper trading.

That is the whole minimum viable setup: **one broker, paper mode, free data, no
paid AI.** Add more venues, feeds, and AI providers later — the system adapts
without a rebuild.

---

## Standard / Local AI (Docker)

Standard adds FinBERT sentiment and the Postgres/TimescaleDB stack:

```bash
pip install -r requirements.txt
docker compose up -d            # Postgres + Redis
cp .env.example .env            # leave DB_BACKEND unset (or =postgres); set POSTGRES_* 
python run.py
```

Local AI is Standard plus a local reasoning model: install [Ollama](https://ollama.ai)
and `ollama pull <model>` (see `config/local_llm_catalogue.yaml` for the
machine-matched recommendation). Local models download **only** when you choose
this profile.

---

## Does it need to run 24/7?

Only for autonomous trading. While the host is off, myTbot cannot ingest data,
evaluate strategies, monitor risk, reconcile positions, or place orders. A
broker-native stop order placed at the broker survives, but myTbot's own
monitoring does not. For always-on operation run it on a dedicated home machine
or your own cloud server (`docs/DEPLOYMENT_MODEL.md`).

---

## Backup & restore

**What to back up:** your `.env` (secrets — store securely, never commit) and
your database.

### Lite (SQLite)
The whole database is one file. Stop myTbot first so the file is quiescent:

```bash
# back up
cp data/mytbot.db backups/mytbot-$(date +%Y%m%d).db
cp .env backups/.env.bak            # secrets — keep encrypted / offline

# restore
cp backups/mytbot-YYYYMMDD.db data/mytbot.db
```

### Standard (Postgres)
Use `pg_dump` / `pg_restore` against the Docker Postgres:

```bash
# back up
docker compose exec -T db pg_dump -U mytbot mytbot > backups/mytbot-$(date +%Y%m%d).sql

# restore (into a fresh, empty database)
docker compose exec -T db psql -U mytbot -d mytbot < backups/mytbot-YYYYMMDD.sql
```

The feature store and price history can also be rebuilt from scratch with
`python run_pipeline.py --backfill` instead of restoring them.

### Migrating between machines / to a server
Never leave **two** myTbot instances armed against the **same broker account**.
Before moving: stop the old instance, confirm it is no longer armed, **rotate or
move the broker API credentials** to the new host, deploy there, run the
read-only connection + balance checks, re-arm **paper** first, and only then
confirm live. See `docs/DEPLOYMENT_MODEL.md` §8 for the full migration checklist.
