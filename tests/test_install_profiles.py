"""M11 install-profile recommender — config-driven Lite/Standard/Local-AI."""
from __future__ import annotations

from connectors.install_profiles import load_profiles, recommend_profile


def test_catalogue_loads_three_profiles():
    cat = load_profiles()
    assert set(cat.get("profiles", {})) == {"lite", "standard", "local_ai"}


def test_low_spec_machine_gets_lite():
    probe = {"ram_gb": 4, "disk_free_gb": 3, "accelerated": False}
    rec = recommend_profile(probe)
    assert rec["recommended"] == "lite"
    assert rec["profiles"]["standard"]["eligible"] is False


def test_capable_cpu_box_gets_standard_not_local_ai():
    # 16GB RAM, plenty of disk, but no GPU and below the CPU fallback (32GB).
    probe = {"ram_gb": 16, "disk_free_gb": 50, "accelerated": False, "vram_gb": 0}
    rec = recommend_profile(probe)
    assert rec["recommended"] == "standard"
    assert rec["profiles"]["local_ai"]["eligible"] is False


def test_gpu_workstation_gets_local_ai():
    probe = {"ram_gb": 32, "disk_free_gb": 200, "accelerated": True, "vram_gb": 12}
    rec = recommend_profile(probe)
    assert rec["recommended"] == "local_ai"
    assert rec["profiles"]["local_ai"]["eligible"] is True


def test_beefy_cpu_only_box_allows_local_ai_fallback():
    # No GPU but 64GB RAM clears the CPU-fallback gate.
    probe = {"ram_gb": 64, "disk_free_gb": 100, "accelerated": False, "vram_gb": 0}
    rec = recommend_profile(probe)
    assert rec["recommended"] == "local_ai"


def test_lite_is_safe_default_on_empty_probe():
    rec = recommend_profile({})
    assert rec["recommended"] == "lite"


def test_profile_metadata_surfaced():
    rec = recommend_profile({"ram_gb": 4, "disk_free_gb": 3})
    lite = rec["profiles"]["lite"]
    assert lite["requirements"] == "requirements-lite.txt"
    assert lite["db_backend"] == "sqlite"
    assert lite["docker_required"] is False
