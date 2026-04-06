# M2 readiness notes

## M1 verification (summary)

The foundation milestone meets the intended bar:

| Area | Notes |
|------|--------|
| `BrokerAdapter` (15 methods) | `brokers/base.py` — frozen interface |
| Adapters | IBKR, Kraken, Binance, Alpaca + `brokers/_template/` |
| Money types | `Decimal` in domain models; SDK floats isolated in adapters |
| `paper_mode=True` default | Adapters + `APP_ENV` in `main.py` |
| Ingestion | `main.py` dual concurrent streams + optional Postgres ticks |
| Persistence | `storage/db.py`, Docker TimescaleDB image |
| Docker | `docker-compose.yml` (db + redis) |
| Logging | `loguru` in adapters / `main` / storage |
| Degradation | DB optional; Kraken lazy SDK import; IBKR connect failures skip stream |
| Extensibility | Registry one-liner pattern |

## Implemented toward M2 (this doc / repo)

1. **Alembic** — `alembic.ini`, `alembic/env.py` (URL from `POSTGRES_*`), initial revision `b611a4f88c2b` (creates schema on empty DB; no-op if tables exist).
2. **Pytest** — `pytest.ini`, `tests/conftest.py`, `tests/test_smoke.py`; deps: `pytest`, `pytest-asyncio`, `psycopg2-binary` (Alembic CLI).
3. **Typing markers** — `brokers/py.typed`, `storage/py.typed` (PEP 561 hooks for checkers).
4. **M2 data pipeline** — revision `d4e8f1a20002` adds `feature_snapshots`, `news_headlines`, `macro_observations` when missing; `config/data_pipeline.yaml`, `data/*`, `run_pipeline.py`; `websockets>=13` for yfinance compatibility.

**Commands**

```bash
# Migrations (sync URL; load .env for POSTGRES_*)
alembic upgrade head

# Tests
pytest
```

**Existing DB from `create_all`:** if tables already exist, the initial revision’s `upgrade()` skips DDL. To align Alembic history only: `alembic stamp head` (optional).

**Downgrade:** initial revision’s `downgrade()` calls `metadata.drop_all()` — use only on disposable databases.

## Suggested next (not done here)

Prioritised roughly by impact vs effort:

1. **Stream watchdog / heartbeat** — detect stalled `stream_prices`, restart or alert (needed before unattended M2 ingestion).
2. **Backoff on transient API errors** — shared retry with jitter for REST polling (429/503).
3. **Native WebSockets** (Kraken/Binance) — lower latency, fewer rate-limit hits than polling.
4. **Richer pytest fixtures** — mocked broker responses, transactional test DB, CI job running `pytest`.
5. **Unify logging** — bridge stdlib loggers in `risk/` / `signals/` / `execution/` to loguru when those modules go live.
6. **Static typing CI** — `mypy` or `pyright` on `brokers/` + `storage/` (incremental strictness).
7. **Lockfile** — `uv lock` / `pip-tools` / pinned `requirements.lock` for reproducible installs.

These stay as M2/M3 work unless pulled into a dedicated task.
