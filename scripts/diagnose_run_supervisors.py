#!/usr/bin/env python3
"""Read-only diagnostic for external ``run.py`` respawners.

Checks the current process list, Windows Task Scheduler, and Startup folders
for commands that reference this repo's run.py. It does not stop or change
anything; use it before live trading to identify IDE/shell/scheduler wrappers.
"""

from __future__ import annotations

import csv
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_PY = (ROOT / "run.py").resolve()


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT, timeout=20)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {exc}"


def _contains_run_py(text: str) -> bool:
    t = text.replace("\\", "/").lower()
    return "run.py" in t and (
        str(RUN_PY).replace("\\", "/").lower() in t
        or str(ROOT).replace("\\", "/").lower() in t
        or "mytbot" in t
    )


def process_matches() -> list[str]:
    if sys.platform != "win32":
        out = _run(["ps", "-eo", "pid,ppid,command"])
        return [line for line in out.splitlines() if _contains_run_py(line)]
    ps = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
        "ConvertTo-Csv -NoTypeInformation"
    )
    out = _run(["powershell", "-NoProfile", "-Command", ps])
    rows = list(csv.DictReader(StringIO(out)))
    return [
        f"pid={r.get('ProcessId')} ppid={r.get('ParentProcessId')} name={r.get('Name')} cmd={r.get('CommandLine')}"
        for r in rows
        if _contains_run_py(str(r.get("CommandLine") or ""))
    ]


def scheduled_task_matches() -> list[str]:
    if sys.platform != "win32":
        return []
    out = _run(["schtasks", "/query", "/fo", "csv", "/v"])
    rows = list(csv.DictReader(StringIO(out)))
    matches: list[str] = []
    for r in rows:
        task = str(r.get("TaskName") or "")
        action = str(r.get("Task To Run") or "")
        if _contains_run_py(f"{task} {action}"):
            matches.append(f"task={task} action={action}")
    return matches


def startup_folder_matches() -> list[str]:
    if sys.platform != "win32":
        return []
    folders = [
        Path(os.getenv("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup",
        Path(os.getenv("ProgramData", "")) / "Microsoft/Windows/Start Menu/Programs/StartUp",
    ]
    out: list[str] = []
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            try:
                text = path.read_text(errors="ignore") if path.is_file() else ""
            except Exception:  # noqa: BLE001
                text = ""
            if _contains_run_py(f"{path} {text}"):
                out.append(str(path))
    return out


def main() -> int:
    print(f"repo={ROOT}")
    print(f"run_py={RUN_PY}")
    sections = {
        "processes": process_matches(),
        "scheduled_tasks": scheduled_task_matches(),
        "startup_folders": startup_folder_matches(),
    }
    found = False
    for name, rows in sections.items():
        print(f"\n[{name}]")
        if not rows:
            print("none")
            continue
        found = True
        for row in rows:
            print(row)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
