#!/usr/bin/env python3
"""
run.py — The single entry point for mytbot.

    python run.py

That's it. This starts:
  1. The orchestrator (infrastructure, brokers, trading loop)
  2. The API server (for UI control)
  3. Everything auto-starts, auto-connects, auto-heals.

Press Ctrl+C to stop everything gracefully.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys

from dotenv import load_dotenv
from loguru import logger


def _configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logger.remove()
    logger.add(sys.stderr, level=level, format=(
        "<green>{time:HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan> | "
        "<level>{message}</level>"
    ))


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def _try_free_port(port: int) -> None:
    """Best-effort attempt to kill the process holding *port*."""
    if sys.platform == "win32":
        try:
            out = subprocess.check_output(
                ["netstat", "-ano"], text=True, timeout=5
            )
            for line in out.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid],
                        timeout=5, capture_output=True,
                    )
                    logger.info("killed stale PID {} on port {}", pid, port)
        except Exception as exc:
            logger.warning("could not kill process on port {}: {}", port, exc)
    else:
        try:
            subprocess.run(
                ["fuser", "-k", f"{port}/tcp"],
                timeout=5, capture_output=True,
            )
        except Exception:
            pass


def _resolve_port(preferred: int) -> int:
    """Return *preferred* if available, otherwise find the next free port."""
    if _port_is_free(preferred):
        return preferred

    logger.warning("port {} in use — attempting to free it", preferred)
    _try_free_port(preferred)

    import time
    for _ in range(6):
        time.sleep(0.5)
        if _port_is_free(preferred):
            logger.info("port {} freed successfully", preferred)
            return preferred

    for candidate in range(preferred + 1, preferred + 20):
        if _port_is_free(candidate):
            logger.warning(
                "port {} stuck (zombie socket) — using port {} instead",
                preferred, candidate,
            )
            return candidate

    return preferred


async def _run() -> None:
    from system.orchestrator import Orchestrator

    _orch = Orchestrator.get_instance()
    logger.info("mytbot | orchestrator ready (state=off) — use dashboard to start")

    import uvicorn

    api_port = _resolve_port(int(os.getenv("API_PORT", "8000")))
    api_host = os.getenv("API_HOST", "0.0.0.0")
    logger.info("mytbot | API → http://{}:{}", api_host, api_port)

    config = uvicorn.Config(
        "api.server:app",
        host=api_host,
        port=api_port,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
        reload=False,
    )
    server = uvicorn.Server(config)

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("mytbot | shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, sig_name), _signal_handler)
        except (NotImplementedError, AttributeError, ValueError):
            pass  # Windows: SIGINT handled via KeyboardInterrupt

    api_task = asyncio.create_task(server.serve(), name="api-server")

    try:
        done, _ = await asyncio.wait(
            [asyncio.create_task(stop_event.wait()), api_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            exc = task.exception() if not task.cancelled() else None
            if isinstance(exc, SystemExit):
                logger.error("mytbot | API server failed to start (port conflict?)")
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except SystemExit:
        logger.error("mytbot | API server exited unexpectedly")

    logger.info("mytbot | shutting down...")
    orch = Orchestrator.get_instance()
    if orch.state.value != "off":
        await orch.stop()
    server.should_exit = True
    try:
        await asyncio.wait_for(api_task, timeout=10)
    except (asyncio.TimeoutError, asyncio.CancelledError, SystemExit):
        api_task.cancel()
    logger.info("mytbot | goodbye")


def main() -> None:
    load_dotenv()
    _configure_logging()
    logger.info("mytbot | one-button trading system")
    logger.info("mytbot | press Ctrl+C to stop")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("mytbot | interrupted — exiting")
    except SystemExit:
        pass


if __name__ == "__main__":
    main()
