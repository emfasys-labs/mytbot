"""
scripts/auto_train_models.py
============================
Research/paper-only model training conductor.

The repo already has individual trainers for meta-labels, forecasts, regime
models, microstructure, and deep-sequence experiments. This script is the
auditable wrapper that runs them one by one from config/auto_training.yaml.

It never promotes a model to micro_live/live. By default it trains artefacts and
writes reports. Paper registration is intentionally a separate, explicit flag.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import yaml
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
except Exception:  # noqa: BLE001
    load_dotenv = None

from storage.db import dispose_engine, init_async_database  # noqa: E402
from storage.models import FeatureSnapshot  # noqa: E402


DEFAULT_CONFIG = ROOT / "config" / "auto_training.yaml"


@dataclass
class CommandResult:
    name: str
    command: list[str]
    returncode: int | None
    skipped: bool = False
    reason: str | None = None


def _load_env() -> None:
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env")


def _load_config(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = raw.get("auto_training") or {}
    if not isinstance(cfg, dict):
        raise SystemExit("auto_training config must be a mapping")
    return cfg


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _cmd(*parts: str) -> list[str]:
    return [sys.executable, *parts]


def _run_command(name: str, command: list[str], *, dry_run: bool) -> CommandResult:
    if dry_run:
        return CommandResult(name=name, command=command, returncode=None)
    proc = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    output = "\n".join(part.strip() for part in (proc.stdout, proc.stderr) if part and part.strip())
    if output:
        print(output[-4000:])
    if int(proc.returncode) == 2:
        reason = (output.splitlines()[-1] if output else "insufficient_training_data")[:500]
        return CommandResult(
            name=name,
            command=command,
            returncode=2,
            skipped=True,
            reason=reason,
        )
    return CommandResult(name=name, command=command, returncode=int(proc.returncode))


async def _export_forecast_csvs(
    *,
    symbol: str,
    timeframe: str,
    out_dir: Path,
    min_rows: int,
) -> tuple[Path | None, Path | None, str | None]:
    engine, session_factory = await init_async_database()
    if session_factory is None:
        return None, None, "database_unavailable"
    try:
        async with session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(FeatureSnapshot)
                        .where(FeatureSnapshot.symbol == symbol)
                        .where(FeatureSnapshot.timeframe == timeframe)
                        .order_by(FeatureSnapshot.bar_timestamp.asc())
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await dispose_engine(engine)

    if len(rows) < int(min_rows):
        return None, None, f"insufficient_rows:{len(rows)}<{min_rows}"

    out_dir.mkdir(parents=True, exist_ok=True)
    idx = pd.DatetimeIndex([r.bar_timestamp for r in rows])
    close = pd.DataFrame({"close": [float(r.close) for r in rows]}, index=idx)
    feature_rows: list[dict[str, float]] = []
    for row in rows:
        raw = dict(row.features or {})
        feature_rows.append({
            k: float(v)
            for k, v in raw.items()
            if isinstance(v, (int, float)) and pd.notna(v)
        })
    feats = pd.DataFrame(feature_rows, index=idx)
    # Keep only numeric columns that are complete enough for the existing
    # trainer. This is intentionally conservative: no forward/back filling.
    feats = feats.select_dtypes(include=["number"]).dropna(axis=1)
    if feats.empty:
        return None, None, "no_numeric_features"

    close_path = out_dir / f"{symbol}_{timeframe}_close.csv"
    feats_path = out_dir / f"{symbol}_{timeframe}_features.csv"
    close.to_csv(close_path)
    feats.to_csv(feats_path)
    return close_path, feats_path, None


def _meta_label_commands(cfg: dict[str, Any], run_id: str) -> Iterable[tuple[str, list[str]]]:
    jobs = cfg.get("jobs") or {}
    job = jobs.get("meta_labeler") or {}
    if not job.get("enabled", False):
        return []
    research_root = Path(str(cfg.get("research_root", "data/research")))
    output_root = Path(str(cfg.get("output_root", "artifacts/models")))
    model_name = str(job.get("model_name", "mytbot_meta_labeler"))
    data_dir = research_root / "meta_label" / run_id
    artefact = output_root / "meta_label" / f"{model_name}-{run_id}.pkl"
    build = _cmd(
        "scripts/build_meta_label_dataset.py",
        "--out-dir", str(data_dir),
        "--timeframe", str(job.get("timeframe", "1h")),
        "--horizon-bars", str(job.get("horizon_bars", 10)),
        "--min-rows", str(job.get("min_rows", 200)),
    )
    train = _cmd(
        "scripts/train_meta_labeler.py",
        "--features", str(data_dir / "features.csv"),
        "--labels", str(data_dir / "labels.csv"),
        "--out", str(artefact),
        "--classifier", str((job.get("classifier_candidates") or ["logreg"])[0]),
        "--calibration", str(job.get("calibration", "platt")),
    )
    return [("meta_labeler.build_dataset", build), ("meta_labeler.train", train)]


async def _forecast_commands(cfg: dict[str, Any], run_id: str) -> list[CommandResult | tuple[str, list[str]]]:
    jobs = cfg.get("jobs") or {}
    job = jobs.get("forecasts") or {}
    if not job.get("enabled", False):
        return []
    research_root = Path(str(cfg.get("research_root", "data/research")))
    output_root = Path(str(cfg.get("output_root", "artifacts/models")))
    timeframe = str(job.get("timeframe", "1h"))
    min_rows = int(job.get("min_rows", 300))
    regression_estimator = str((job.get("estimator_candidates") or ["ridge"])[0])
    classifier = str((job.get("classifier_candidates") or ["logreg"])[0])
    calibration = str(job.get("calibration", "none"))
    out: list[CommandResult | tuple[str, list[str]]] = []
    for target in job.get("targets") or []:
        model_name = str(target["model_name"])
        symbol = str(target.get("symbol", "SPY"))
        work_dir = research_root / "forecasts" / run_id / model_name
        close_csv, feats_csv, reason = await _export_forecast_csvs(
            symbol=symbol,
            timeframe=timeframe,
            out_dir=work_dir,
            min_rows=min_rows,
        )
        if reason:
            out.append(CommandResult(name=f"{model_name}.train", command=[], returncode=None, skipped=True, reason=reason))
            continue
        artefact = output_root / "forecasts" / f"{model_name}-{run_id}.pkl"
        target_kind = str(target["target_kind"])
        estimator = (
            classifier
            if target_kind in {
                "breakout_continuation",
                "mean_reversion_success",
                "drawdown_probability",
            }
            else regression_estimator
        )
        out.append((
            f"{model_name}.train",
            _cmd(
                "scripts/train_forecasts.py",
                "--close", str(close_csv),
                "--features", str(feats_csv),
                "--target", target_kind,
                "--horizon", str(target["horizon"]),
                "--estimator", estimator,
                "--calibration", calibration,
                "--out", str(artefact),
            ),
        ))
    return out


def _regime_commands(cfg: dict[str, Any], run_id: str) -> Iterable[CommandResult | tuple[str, list[str]]]:
    jobs = cfg.get("jobs") or {}
    job = jobs.get("regime_classifier") or {}
    if not job.get("enabled", False):
        return []
    features = Path(str(job.get("features_csv", "")))
    if not features.exists():
        return [CommandResult(name="regime_classifier.train", command=[], returncode=None, skipped=True, reason="features_csv_missing")]
    output_root = Path(str(cfg.get("output_root", "artifacts/models")))
    artefact = output_root / "regime" / f"regime_classifier-{run_id}.pkl"
    return [("regime_classifier.train", _cmd("scripts/refit_regime_models.py", "--features", str(features), "--out", str(artefact)))]


def _microstructure_commands(cfg: dict[str, Any], run_id: str) -> Iterable[CommandResult | tuple[str, list[str]]]:
    jobs = cfg.get("jobs") or {}
    job = jobs.get("microstructure") or {}
    if not job.get("enabled", False):
        return []
    src = Path(str(job.get("input_csv", "")))
    if not src.exists():
        return [CommandResult(name="microstructure.train", command=[], returncode=None, skipped=True, reason="input_csv_missing")]
    output_root = Path(str(cfg.get("output_root", "artifacts/models")))
    artefact = output_root / "microstructure" / f"imbalance-{run_id}.pkl"
    return [("microstructure.train", _cmd("scripts/evaluate_microstructure.py", "--input", str(src), "--out", str(artefact)))]


def _write_report(cfg: dict[str, Any], run_id: str, results: list[CommandResult], *, dry_run: bool) -> Path:
    report_root = Path(str(cfg.get("report_root", "reports/models")))
    out_dir = report_root / "auto_training"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run_id}.json"
    payload = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "results": [
            {
                "name": r.name,
                "command": r.command,
                "returncode": r.returncode,
                "skipped": r.skipped,
                "reason": r.reason,
            }
            for r in results
        ],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


async def _run(args: argparse.Namespace) -> int:
    _load_env()
    cfg = _load_config(Path(args.config))
    if not bool(cfg.get("enabled", False)):
        print("auto_training disabled in config")
        return 0
    if bool((cfg.get("safety") or {}).get("allow_live_promotion", False)):
        raise SystemExit("refusing to run: allow_live_promotion must remain false")

    run_id = args.run_id or _ts()
    dry_run = bool(args.dry_run)
    plan: list[CommandResult | tuple[str, list[str]]] = []
    plan.extend(_meta_label_commands(cfg, run_id))
    plan.extend(await _forecast_commands(cfg, run_id))
    plan.extend(_regime_commands(cfg, run_id))
    plan.extend(_microstructure_commands(cfg, run_id))

    results: list[CommandResult] = []
    unavailable_dependencies: set[str] = set()
    for item in plan:
        if isinstance(item, CommandResult):
            results.append(item)
            print(f"SKIP {item.name}: {item.reason}")
            continue
        name, command = item
        if name == "meta_labeler.train" and "meta_labeler.build_dataset" in unavailable_dependencies:
            result = CommandResult(
                name=name,
                command=command,
                returncode=None,
                skipped=True,
                reason="dependency_not_ready:meta_labeler.build_dataset",
            )
            results.append(result)
            print(f"SKIP {name}: {result.reason}")
            continue
        print(f"RUN  {name}: {' '.join(command)}")
        if args.plan_only:
            results.append(CommandResult(name=name, command=command, returncode=None))
        else:
            result = _run_command(name, command, dry_run=dry_run)
            results.append(result)
            if name == "meta_labeler.build_dataset" and (
                result.skipped or result.returncode not in (None, 0)
            ):
                unavailable_dependencies.add(name)

    report = _write_report(cfg, run_id, results, dry_run=dry_run or args.plan_only)
    print(f"auto_training report: {report}")
    failed = [r for r in results if r.returncode not in (None, 0) and not r.skipped]
    return 1 if failed else 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run configured research/paper model training jobs")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--run-id", default=None)
    p.add_argument("--plan-only", action="store_true", help="print commands and write report, do not execute")
    p.add_argument("--dry-run", action="store_true", help="same as plan-only for external commands")
    return p.parse_args()


def main() -> int:
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
