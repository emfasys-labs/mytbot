from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_auto_training_config_is_paper_only() -> None:
    raw = yaml.safe_load((ROOT / "config" / "auto_training.yaml").read_text(encoding="utf-8"))
    cfg = raw["auto_training"]
    assert cfg["enabled"] is True
    assert cfg["mode"] == "paper"
    assert cfg["safety"]["allow_live_promotion"] is False
    assert cfg["jobs"]["meta_labeler"]["enabled"] is True
    assert cfg["jobs"]["forecasts"]["enabled"] is True


def test_auto_training_script_refuses_live_promotion_setting() -> None:
    src = (ROOT / "scripts" / "auto_train_models.py").read_text(encoding="utf-8")
    assert "allow_live_promotion" in src
    assert "refusing to run" in src
    assert "micro_live" in src
    assert "live" in src


def test_auto_training_plan_only_writes_json_report(tmp_path, monkeypatch) -> None:
    cfg_path = tmp_path / "auto_training.yaml"
    report_root = tmp_path / "reports"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "auto_training": {
                    "enabled": True,
                    "mode": "paper",
                    "output_root": str(tmp_path / "artifacts"),
                    "research_root": str(tmp_path / "research"),
                    "report_root": str(report_root),
                    "safety": {"allow_live_promotion": False},
                    "jobs": {
                        "meta_labeler": {"enabled": False},
                        "forecasts": {"enabled": False},
                        "regime_classifier": {"enabled": False},
                        "microstructure": {"enabled": False},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    import scripts.auto_train_models as atm

    class Args:
        config = str(cfg_path)
        run_id = "test_run"
        plan_only = True
        dry_run = False

    rc = __import__("asyncio").run(atm._run(Args()))
    assert rc == 0
    report = report_root / "auto_training" / "test_run.json"
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["run_id"] == "test_run"
    assert payload["dry_run"] is True
    assert payload["results"] == []
