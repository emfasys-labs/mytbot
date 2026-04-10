"""Verify API can reach Postgres using the same env vars as storage/db.py.

Exit 0 on success, 1 on failure. Prints a short reason (no secrets).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

try:
    import asyncpg
except ImportError:
    print("verify_db | asyncpg not installed (pip install asyncpg)")
    sys.exit(1)


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:
        pass


async def _main() -> int:
    _load_dotenv()
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    user = os.getenv("POSTGRES_USER", "mytbot")
    db = os.getenv("POSTGRES_DB", "mytbot")
    password = os.getenv("POSTGRES_PASSWORD", "")
    try:
        conn = await asyncpg.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=db,
            timeout=5,
        )
        ver = await conn.fetchval("SELECT version()")
        await conn.close()
        print("verify_db | OK |", (ver or "")[:60])
        return 0
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        print("verify_db | FAIL |", type(exc).__name__, "|", str(exc)[:200])
        if "password authentication failed" in msg:
            print(
                "verify_db | hint | If Docker Postgres is on another host port, set POSTGRES_PORT in .env "
                "(e.g. 5433) so it does not hit a native Windows Postgres on 5432."
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
