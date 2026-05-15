#!/usr/bin/env python3
"""
scripts/supervise.py — restart-safe process supervisor for run.py (B2).

Why this exists
---------------
``python run.py`` is the one-button entry point, but on its own it does NOT
survive a crash, and historically it was launched by ad-hoc shells/IDE
runners that detached — so the live process could die (or its logs vanish)
with no recovery and no audit trail. For real-money operation that is
unacceptable.

This supervisor:
  * runs exactly one ``run.py`` (single-instance guard via PID file + an
    optional TCP port probe) — no duplicate trading processes;
  * restarts it on *crash* (non-zero exit) with exponential backoff;
  * does NOT restart on a clean exit (code 0) or on Ctrl+C/SIGTERM to the
    supervisor — a deliberate stop stays stopped;
  * has a crash-loop circuit breaker: too many rapid restarts ⇒ it gives
    up and exits non-zero rather than hammering brokers forever;
  * forwards SIGINT/SIGTERM to the child for a graceful shutdown;
  * writes its own decisions to logs/supervisor.log (run.py self-logs to
    logs/mytbot.log via its rotating file sink).

Safe interaction with the risk kill switch (B1a)
------------------------------------------------
The risk engine persists ``is_killed`` to data/runtime/risk_state.json and
the orchestrator no longer auto-clears it on start (it latches + logs
CRITICAL). Therefore an auto-restart by THIS supervisor is safe: a bot that
tripped its own kill switch comes back up, restores ``is_killed=True``, and
places no orders until a human deliberately clears it. The supervisor must
never touch the kill switch or that state file — and it doesn't.

Usage
-----
    python scripts/supervise.py
    MYTBOT_SUPERVISE_MAX_RAPID=5 MYTBOT_SUPERVISE_RAPID_WINDOW=180 \
        python scripts/supervise.py

Stop with Ctrl+C (graceful) — the child gets a chance to flatten/clean up.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_PY = ROOT / "run.py"
LOGS = ROOT / "logs"
PID_FILE = LOGS / "supervisor.pid"

BASE_BACKOFF_SEC = float(os.getenv("MYTBOT_SUPERVISE_BACKOFF_BASE", "5"))
MAX_BACKOFF_SEC = float(os.getenv("MYTBOT_SUPERVISE_BACKOFF_CAP", "300"))
MAX_RAPID = int(os.getenv("MYTBOT_SUPERVISE_MAX_RAPID", "5"))
RAPID_WINDOW_SEC = float(os.getenv("MYTBOT_SUPERVISE_RAPID_WINDOW", "180"))
API_PORT = int(os.getenv("PORT", os.getenv("API_PORT", "8000")) or 8000)

_stop = False


def _build_logger() -> logging.Logger:
    LOGS.mkdir(parents=True, exist_ok=True)
    lg = logging.getLogger("supervise")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | supervise | %(message)s")
    fh = RotatingFileHandler(LOGS / "supervisor.log", maxBytes=5_000_000, backupCount=5)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    lg.addHandler(fh)
    lg.addHandler(sh)
    return lg


log = _build_logger()


def _port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def _single_instance_guard() -> None:
    if PID_FILE.exists():
        try:
            other = int(PID_FILE.read_text().strip() or "0")
        except (ValueError, OSError):
            other = 0
        if other and other != os.getpid() and _pid_alive(other):
            log.error("another supervisor (pid=%s) is already running — exiting", other)
            sys.exit(2)
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def _handle_signal(signum, _frame) -> None:  # noqa: ANN001
    global _stop
    _stop = True
    log.warning("supervisor received signal %s — stopping (no further restarts)", signum)


def main() -> int:
    _single_instance_guard()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass

    if _port_busy(API_PORT):
        log.error(
            "port %s already busy — a run.py may already be live. "
            "Refusing to start a second trading process.", API_PORT,
        )
        return 2

    log.info("supervising: %s %s", sys.executable, RUN_PY)
    log.info(
        "policy: backoff %.0fs→%.0fs, circuit-break at %d restarts / %.0fs",
        BASE_BACKOFF_SEC, MAX_BACKOFF_SEC, MAX_RAPID, RAPID_WINDOW_SEC,
    )

    restart_times: list[float] = []
    consecutive_failures = 0
    child: subprocess.Popen | None = None

    try:
        while not _stop:
            started = time.monotonic()
            child = subprocess.Popen(  # noqa: S603
                [sys.executable, str(RUN_PY)],
                cwd=str(ROOT),
            )
            log.info("run.py started (pid=%s)", child.pid)

            # Wait, forwarding a stop signal to the child if we get one.
            while True:
                try:
                    code = child.wait(timeout=2)
                    break
                except subprocess.TimeoutExpired:
                    if _stop:
                        log.info("forwarding stop to run.py (pid=%s)", child.pid)
                        child.terminate()
                        try:
                            code = child.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            log.warning("run.py did not exit in 30s — killing")
                            child.kill()
                            code = child.wait()
                        break

            ran_for = time.monotonic() - started
            if _stop:
                log.info("stopped by operator (run.py exit=%s)", code)
                return 0
            if code == 0:
                log.info("run.py exited cleanly (0) after %.0fs — not restarting", ran_for)
                return 0

            # Crash path.
            consecutive_failures += 1
            now = time.monotonic()
            restart_times = [t for t in restart_times if now - t < RAPID_WINDOW_SEC]
            restart_times.append(now)
            if len(restart_times) > MAX_RAPID:
                log.critical(
                    "CIRCUIT BREAKER: %d restarts within %.0fs — crash loop. "
                    "Giving up. Investigate logs/mytbot.log before restarting.",
                    len(restart_times), RAPID_WINDOW_SEC,
                )
                return 3

            backoff = min(MAX_BACKOFF_SEC, BASE_BACKOFF_SEC * (2 ** (consecutive_failures - 1)))
            if ran_for > RAPID_WINDOW_SEC:
                # It ran a long time before dying — treat as a fresh incident.
                consecutive_failures = 1
                backoff = BASE_BACKOFF_SEC
            log.error(
                "run.py crashed (exit=%s) after %.0fs — restart #%d in %.0fs",
                code, ran_for, consecutive_failures, backoff,
            )
            slept = 0.0
            while slept < backoff and not _stop:
                time.sleep(min(2.0, backoff - slept))
                slept += 2.0
        return 0
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
        try:
            if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
                PID_FILE.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
