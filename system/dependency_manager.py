"""
system/dependency_manager.py
============================
Auto-start and health-check infrastructure dependencies (Postgres, Redis).
If Docker services are not running, start them automatically.
If already running, reuse.  Never crash — degrade gracefully.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass
class ServiceStatus:
    name: str
    healthy: bool = False
    was_started: bool = False
    error: str | None = None


@dataclass
class DependencyReport:
    postgres: ServiceStatus = field(default_factory=lambda: ServiceStatus(name="postgres"))
    redis: ServiceStatus = field(default_factory=lambda: ServiceStatus(name="redis"))

    @property
    def all_healthy(self) -> bool:
        return self.postgres.healthy and self.redis.healthy

    def to_dict(self) -> dict[str, Any]:
        return {
            "postgres": {"healthy": self.postgres.healthy, "was_started": self.postgres.was_started, "error": self.postgres.error},
            "redis": {"healthy": self.redis.healthy, "was_started": self.redis.was_started, "error": self.redis.error},
        }


def _docker_available() -> bool:
    return shutil.which("docker") is not None


async def _run(cmd: str, timeout: float = 60) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")
    except asyncio.TimeoutError:
        return -1, "", "timeout"
    except Exception as exc:
        return -1, "", str(exc)


async def _is_container_running(name: str) -> bool:
    code, out, _ = await _run(f'docker inspect -f "{{{{.State.Running}}}}" {name}')
    return code == 0 and "true" in out.lower()


async def _is_container_healthy(name: str) -> bool:
    code, out, _ = await _run(f'docker inspect -f "{{{{.State.Health.Status}}}}" {name}')
    return code == 0 and "healthy" in out.lower()


async def _wait_healthy(name: str, timeout: float = 45) -> bool:
    elapsed = 0.0
    while elapsed < timeout:
        if await _is_container_healthy(name):
            return True
        await asyncio.sleep(2)
        elapsed += 2
    return await _is_container_running(name)


async def _start_docker_service(service: str, compose_dir: str) -> tuple[bool, str]:
    code, out, err = await _run(f'docker compose up -d {service}', timeout=90)
    if code != 0:
        return False, err or out
    return True, ""


async def _check_postgres_direct() -> bool:
    """Try a TCP connect to the Postgres port to see if it's reachable."""
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=5
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


async def _check_redis_direct() -> bool:
    """Try a TCP connect to the Redis port."""
    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=5
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


class DependencyManager:
    """Ensures Postgres and Redis are available, starting Docker containers if needed."""

    def __init__(self, compose_dir: str | None = None):
        self.compose_dir = compose_dir or os.getcwd()
        self._has_docker = _docker_available()

    async def ensure_all(self) -> DependencyReport:
        report = DependencyReport()

        pg, rd = await asyncio.gather(
            self._ensure_postgres(report),
            self._ensure_redis(report),
        )

        return report

    async def _ensure_postgres(self, report: DependencyReport) -> None:
        status = report.postgres

        if await _check_postgres_direct():
            status.healthy = True
            logger.info("deps | postgres | already reachable")
            return

        if not self._has_docker:
            status.error = "Postgres unreachable and Docker not found"
            logger.warning("deps | postgres | {}", status.error)
            return

        container = os.getenv("POSTGRES_CONTAINER_NAME", "mytbot_db")
        if await _is_container_running(container):
            if await _wait_healthy(container, timeout=30):
                status.healthy = True
                logger.info("deps | postgres | container running and healthy")
                return
            status.error = "Container running but unhealthy"
            logger.warning("deps | postgres | {}", status.error)
            return

        logger.info("deps | postgres | starting via docker compose...")
        ok, err = await _start_docker_service("db", self.compose_dir)
        if not ok:
            status.error = f"docker compose up failed: {err}"
            logger.error("deps | postgres | {}", status.error)
            return

        status.was_started = True
        if await _wait_healthy(container, timeout=45):
            status.healthy = True
            logger.info("deps | postgres | started and healthy")
        else:
            if await _check_postgres_direct():
                status.healthy = True
                logger.info("deps | postgres | started (no healthcheck but port reachable)")
            else:
                status.error = "Started but did not become healthy in time"
                logger.warning("deps | postgres | {}", status.error)

    async def _ensure_redis(self, report: DependencyReport) -> None:
        status = report.redis

        if await _check_redis_direct():
            status.healthy = True
            logger.info("deps | redis | already reachable")
            return

        if not self._has_docker:
            status.error = "Redis unreachable and Docker not found"
            logger.warning("deps | redis | {} — system will run without cache", status.error)
            return

        container = os.getenv("REDIS_CONTAINER_NAME", "mytbot_redis")
        if await _is_container_running(container):
            if await _wait_healthy(container, timeout=20):
                status.healthy = True
                logger.info("deps | redis | container running and healthy")
                return
            status.error = "Container running but unhealthy"
            logger.warning("deps | redis | {}", status.error)
            return

        logger.info("deps | redis | starting via docker compose...")
        ok, err = await _start_docker_service("redis", self.compose_dir)
        if not ok:
            status.error = f"docker compose up failed: {err}"
            logger.warning("deps | redis | {} — continuing without cache", status.error)
            return

        status.was_started = True
        if await _wait_healthy(container, timeout=20):
            status.healthy = True
            logger.info("deps | redis | started and healthy")
        else:
            if await _check_redis_direct():
                status.healthy = True
                logger.info("deps | redis | started (no healthcheck but port reachable)")
            else:
                status.error = "Started but did not become healthy in time"
                logger.warning("deps | redis | {} — continuing without cache", status.error)

    async def stop_all(self) -> None:
        """Stop Docker services that we started."""
        if not self._has_docker:
            return
        logger.info("deps | stopping docker services...")
        await _run("docker compose stop", timeout=30)
