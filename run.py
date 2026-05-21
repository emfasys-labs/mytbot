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
import inspect
import logging
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger


class _LoguruInterceptHandler(logging.Handler):
    """D125 — bridge stdlib logging into the loguru file sink.

    Modules like execution.engine, execution.router, execution.planner,
    execution.wave9_runtime, ai.fusion, brokers.permissions, etc. use
    `logging.getLogger(__name__)`. Without this handler their records
    never reached the loguru file sink, so 24+ execution-path log
    statements (EXECUTING / MARKET CLOSED / PAPER FILL / EXEC SKIP /
    SIZING GUARD REJECT) were invisible to post-incident review. The
    handler re-emits each stdlib record through loguru, preserving the
    original level, module name, and stack frame depth so the existing
    formatter renders the right `name:function:line` prefix.
    """

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # Walk the call stack to find the caller outside the logging
        # module — this gives loguru's `{name}:{function}:{line}` format
        # the real source instead of `logging:callHandlers:1762`.
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def _configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logger.remove()
    # Install the stdlib→loguru bridge BEFORE adding sinks so the very
    # first stdlib log line is captured. Replacing the root handlers
    # (force=True) ensures uvicorn/asyncio/etc. don't keep a side
    # console sink that double-emits.
    logging.basicConfig(handlers=[_LoguruInterceptHandler()], level=0, force=True)
    logger.add(sys.stderr, level=level, format=(
        "<green>{time:HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan> | "
        "<level>{message}</level>"
    ))
    # B2 — durable in-process file logging. Previously the only sink was
    # stderr, so when the launching shell/wrapper detached (the exit-255
    # respawn pattern) the live process's logs were unrecoverable and the
    # bot could not be audited while running — unacceptable for a money
    # system. A rotating file sink means the running process ALWAYS writes
    # an auditable log to logs/mytbot.log regardless of how it was started
    # or whether its parent died. Rotation + retention bound disk use; the
    # path is overridable via MYTBOT_LOG_FILE. Failure to open the file
    # must never prevent the system from starting (stderr still works).
    try:
        log_file = os.getenv("MYTBOT_LOG_FILE", "").strip() or str(
            Path(__file__).resolve().parent / "logs" / "mytbot.log"
        )
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_file,
            level=level,
            rotation=os.getenv("MYTBOT_LOG_ROTATION", "50 MB"),
            retention=os.getenv("MYTBOT_LOG_RETENTION", "14 days"),
            compression="zip",
            enqueue=True,          # process-safe, non-blocking under a supervisor
            backtrace=True,
            diagnose=False,        # never leak locals/secrets into the file
            format=(
                "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
                "{name}:{function}:{line} | {message}"
            ),
        )
        logger.info("mytbot | file log → {}", log_file)
    except Exception as exc:  # noqa: BLE001 — logging must never block startup
        logger.warning("mytbot | file logging unavailable ({}); stderr only", exc)


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


def _ui_sources_newer_than_dist(ui_dir: Path) -> bool:
    """True if ui/src changed after the last ui/dist build (or dist is missing)."""
    dist_index = ui_dir / "dist" / "index.html"
    src = ui_dir / "src"
    if not src.is_dir():
        return False
    newest_src = 0.0
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        try:
            newest_src = max(newest_src, p.stat().st_mtime)
        except OSError:
            continue
    if not dist_index.is_file():
        return True
    try:
        return newest_src > dist_index.stat().st_mtime
    except OSError:
        return True


def _ensure_ui_built() -> None:
    """
    The API serves the pre-built SPA from ui/dist. After pulling UI changes, that folder
    must be rebuilt — otherwise a browser refresh still shows old JS. This keeps the
    one-command workflow: python run.py refreshes the bundle when sources are newer than dist.
    Set UI_AUTO_BUILD=0 to skip (e.g. when using Vite dev server separately).
    """
    if os.getenv("UI_AUTO_BUILD", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    root = Path(__file__).resolve().parent
    ui_dir = root / "ui"
    if not (ui_dir / "package.json").is_file():
        return
    if not _ui_sources_newer_than_dist(ui_dir):
        return
    logger.info("mytbot | building dashboard (npm run build in ui/) — one-time when UI changed")
    try:
        completed = subprocess.run(
            ["npm", "run", "build"],
            cwd=str(ui_dir),
            timeout=600,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        logger.warning(
            "mytbot | npm not found — install Node.js or run manually: cd ui && npm run build"
        )
        return
    except subprocess.TimeoutExpired:
        logger.error("mytbot | dashboard build timed out after 600s")
        return
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "")[-2500:]
        logger.warning(
            "mytbot | dashboard build failed (exit {}); using existing ui/dist if any.\n{}",
            completed.returncode,
            tail,
        )
        return
    logger.info("mytbot | dashboard build finished — refresh the browser")


def _resolve_port(preferred: int) -> int:
    """Return *preferred* if available, otherwise find the next free port."""
    if _port_is_free(preferred):
        return preferred

    if os.getenv("APP_ENV", "paper").strip().lower() == "live":
        raise RuntimeError(
            f"API port {preferred} is already in use; refusing live port takeover or alternate-port restart"
        )

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

    # Autostart: with MYTBOT_AUTOSTART truthy, the system comes up RUNNING
    # on every boot — including unattended supervisor crash-recovery
    # restarts — instead of sitting OFF waiting for a human to press START.
    # Safe because the risk kill-switch now PERSISTS across restarts (B1):
    # a tripped kill stays tripped even with autostart, so a bad day cannot
    # be "restarted away". Default off preserves the conservative one-button
    # behaviour for other deployments; opt in via .env.
    async def _maybe_autostart() -> None:
        if os.getenv("MYTBOT_AUTOSTART", "").strip().lower() not in (
            "1", "true", "yes", "on",
        ):
            logger.info("mytbot | orchestrator ready (state=off) — autostart disabled")
            return
        await asyncio.sleep(2)  # let the API bind first
        # Self-healing retry. A supervisor crash-recovery restart after a
        # machine wake can race the infrastructure coming back (Postgres /
        # Docker still settling) — the first start() then lands in ERROR. We
        # must NOT sit dead waiting for a human to press START; retry with
        # backoff until RUNNING (or until the orchestrator is no longer OFF/
        # ERROR, e.g. an operator stopped it, or the kill switch is latched —
        # the orchestrator itself enforces that, autostart never overrides it).
        try:
            attempts = max(1, int(os.getenv("MYTBOT_AUTOSTART_MAX_ATTEMPTS", "30")))
        except (TypeError, ValueError):
            attempts = 30
        try:
            retry_delay = max(2.0, float(os.getenv("MYTBOT_AUTOSTART_RETRY_SEC", "10")))
        except (TypeError, ValueError):
            retry_delay = 10.0
        for attempt in range(1, attempts + 1):
            try:
                state = _orch.state.value
                if state == "running":
                    logger.info("mytbot | autostart complete (state=running)")
                    return
                if state not in ("off", "error"):
                    logger.info("mytbot | autostart skipped (state={})", state)
                    return
                logger.info(
                    "mytbot | MYTBOT_AUTOSTART → starting orchestrator "
                    "(attempt {}/{})", attempt, attempts,
                )
                await _orch.start()
                new_state = _orch.state.value
                if new_state == "running":
                    logger.info("mytbot | autostart complete (state=running)")
                    return
                logger.warning(
                    "mytbot | autostart attempt {}/{} did not reach RUNNING "
                    "(state={}, last_error={}) — retrying in {:.0f}s",
                    attempt, attempts, new_state,
                    getattr(_orch, "_last_start_error", None), retry_delay,
                )
            except Exception as exc:  # noqa: BLE001 — never crash boot on autostart
                logger.error(
                    "mytbot | autostart attempt {}/{} failed: {} — retrying in {:.0f}s",
                    attempt, attempts, exc, retry_delay,
                )
            await asyncio.sleep(retry_delay)
        logger.error(
            "mytbot | autostart exhausted {} attempts without reaching RUNNING "
            "— supervisor/operator intervention required", attempts,
        )

    autostart_task = asyncio.create_task(_maybe_autostart(), name="autostart")

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
    if not autostart_task.done():
        autostart_task.cancel()
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
    _ensure_ui_built()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("mytbot | interrupted — exiting")
    except SystemExit:
        pass


if __name__ == "__main__":
    main()
