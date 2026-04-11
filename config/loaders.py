"""Load and validate allocation / profile-mode YAML into Pydantic models."""

from __future__ import annotations

from pathlib import Path

import yaml

from config.models import AllocationConfig, ProfileModesConfig


def _load_yaml(path: str | Path) -> dict:
    p = Path(path)
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping at root of YAML file: {p}")
    return data


def load_profile_modes(path: str | Path | None = None) -> ProfileModesConfig:
    if path is None:
        path = Path(__file__).resolve().parent / "profile_modes.yaml"
    return ProfileModesConfig.model_validate(_load_yaml(path))


def load_allocation(path: str | Path | None = None) -> AllocationConfig:
    if path is None:
        path = Path(__file__).resolve().parent / "allocation.yaml"
    return AllocationConfig.model_validate(_load_yaml(path))
