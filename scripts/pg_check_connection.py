"""
Quick check: can this machine reach Postgres using .env (same as test_ibkr / app).

Run from repo root:
  python scripts/pg_check_connection.py

On Windows, if this fails with InvalidPasswordError but psql *inside* the Docker
container works, you often have a *second* PostgreSQL (Windows service) on
port 5432. Connections to localhost/127.0.0.1 then hit the wrong server.
Fix: stop the Windows postgresql service, or map Docker to another host port
(e.g. POSTGRES_PORT=5433 in .env and docker compose up -d).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


async def _try(host: str, port: int, user: str, password: str, database: str) -> str:
    import asyncpg

    conn = await asyncpg.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        timeout=10,
    )
    one = await conn.fetchval("SELECT 1")
    await conn.close()
    return f"OK (SELECT 1 = {one})"


def main() -> None:
    load_dotenv(_repo_root() / ".env")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    user = os.getenv("POSTGRES_USER", "mytbot")
    password = os.getenv("POSTGRES_PASSWORD", "")
    database = os.getenv("POSTGRES_DB", "mytbot")

    print(f"From .env: host={host!r} port={port} user={user!r} db={database!r}")
    print(f"POSTGRES_PASSWORD length={len(password)} (value not printed)")

    async def run() -> None:
        try:
            msg = await _try(host, port, user, password, database)
            print(f"Connect ({host}:{port}): {msg}")
        except Exception as exc:  # noqa: BLE001
            print(f"Connect ({host}:{port}): FAILED — {type(exc).__name__}: {exc}")

        if host in ("localhost", "127.0.0.1", "::1"):
            for alt in ("127.0.0.1", "localhost"):
                if alt == host:
                    continue
                try:
                    msg = await _try(alt, port, user, password, database)
                    print(f"Connect ({alt}:{port}): {msg}")
                except Exception as exc:  # noqa: BLE001
                    print(f"Connect ({alt}:{port}): FAILED — {type(exc).__name__}: {exc}")

    asyncio.run(run())


if __name__ == "__main__":
    try:
        main()
    except ImportError as e:
        print("Need asyncpg: pip install asyncpg", file=sys.stderr)
        raise SystemExit(1) from e
