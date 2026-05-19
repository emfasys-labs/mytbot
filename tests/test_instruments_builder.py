"""Tests for ``instruments.builder`` orchestrator (D116)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from instruments.builder import (
    BuilderConfig,
    build_sources,
    load_config,
)
from instruments.registry import SourceContribution
from instruments.sources.base import (
    Source,
    SourceContext,
    SourceFetchError,
    SourceFetchResult,
)


class _FakeSource:
    def __init__(
        self,
        source_id: str,
        *,
        contributions: list[SourceContribution] | None = None,
        raise_error: bool = False,
    ) -> None:
        self.source_id = source_id
        self.source_version = f"{source_id}.test"
        self.cadence_sec = 86400
        self._contributions = contributions or []
        self._raise = raise_error

    async def fetch(self, ctx: SourceContext) -> SourceFetchResult:
        if self._raise:
            raise SourceFetchError("simulated failure")
        return SourceFetchResult(
            source_id=self.source_id,
            source_version=self.source_version,
            contributions=self._contributions,
            fetched_at=ctx.started_at,
            partial=False,
        )


def test_load_config_handles_missing_file(tmp_path) -> None:
    cfg = load_config(tmp_path / "does-not-exist.yaml")
    # Defaults: enabled, retire policy populated, all sources enabled by default
    assert cfg.enabled is True
    assert int(cfg.retire_policy["min_consecutive_misses"]) >= 1
    assert cfg.sources_enabled == {}  # empty == "everything default-on"


def test_load_config_parses_overrides_and_flags(tmp_path) -> None:
    p = tmp_path / "instrument_registry.yaml"
    p.write_text(
        """
enabled: true
ibkr_supported_symbols_use_registry: true
retire:
  min_consecutive_misses: 7
  min_sources_missing: 3
overrides:
  pinned: [AAPL]
  excluded: [XYZ]
sources:
  wikipedia:
    enabled: false
  ishares:
    enabled: true
    enabled_ids: [ishares.IVV]
        """.strip(),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.ibkr_supported_symbols_use_registry is True
    assert cfg.retire_policy["min_consecutive_misses"] == 7
    assert cfg.retire_policy["min_sources_missing"] == 3
    assert "AAPL" in cfg.overrides["pinned"]
    assert "XYZ" in cfg.overrides["excluded"]
    assert cfg.sources_enabled["wikipedia"] is False
    assert cfg.sources_enabled["ishares"] is True
    assert cfg.source_ids_enabled["ishares"] == ("ishares.IVV",)


def test_build_sources_includes_expected_families() -> None:
    cfg = BuilderConfig(
        enabled=True,
        sources_enabled={
            "wikipedia": False,
            "ishares": False,
            "static_fx": True,
            "static_futures": True,
            "broker_catalog": False,
            "openfigi": False,
        },
    )
    sources = build_sources(cfg, broker_manager=None)
    ids = {s.source_id for s in sources}
    assert "static.fx" in ids
    assert "static.futures" in ids
    assert all(not sid.startswith("wikipedia.") for sid in ids)
    assert all(not sid.startswith("ishares.") for sid in ids)


def test_build_sources_select_filters_individual_ids() -> None:
    cfg = BuilderConfig(
        enabled=True,
        sources_enabled={
            "wikipedia": True,
            "ishares": True,
            "static_fx": True,
            "static_futures": True,
            "broker_catalog": False,
            "openfigi": False,
        },
    )
    sources = build_sources(cfg, broker_manager=None, select=["wikipedia.sp500", "static.fx"])
    ids = {s.source_id for s in sources}
    assert ids == {"wikipedia.sp500", "static.fx"}


@pytest.mark.asyncio
async def test_run_one_source_dry_run_records_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dry-run should skip session writes but still surface contributions."""

    from instruments import builder as builder_mod
    from instruments.registry import coerce_contribution

    contrib = coerce_contribution("AAPL")
    assert contrib is not None
    fake_source = _FakeSource("wikipedia.test", contributions=[contrib])
    ctx = SourceContext(started_at=datetime.now(timezone.utc))

    result = await builder_mod._run_one_source(
        session_factory=None,  # type: ignore[arg-type]
        source=fake_source,
        ctx=ctx,
        retire_policy={"min_consecutive_misses": 5, "min_sources_missing": 2},
        dry_run=True,
    )
    assert result.status == "success"
    assert result.rows_added == 1
    assert result.error is None


@pytest.mark.asyncio
async def test_run_one_source_records_failure_in_dry_run() -> None:
    from instruments import builder as builder_mod

    fake_source = _FakeSource("wikipedia.test_fail", raise_error=True)
    ctx = SourceContext(started_at=datetime.now(timezone.utc))
    result = await builder_mod._run_one_source(
        session_factory=None,  # type: ignore[arg-type]
        source=fake_source,
        ctx=ctx,
        retire_policy={},
        dry_run=True,
    )
    assert result.status == "failed"
    assert "simulated failure" in (result.error or "")
