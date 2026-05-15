from __future__ import annotations

from decimal import Decimal

from universe.clustering import cluster_by_correlation
from universe.correlation_graph import correlation_matrix, pearson_correlation
from universe.eligibility import evaluate_eligibility
from universe.persistence import merge_cluster_payload
from universe.promotion_engine import PromotionCandidate, evaluate_promotion
from universe.representative_selector import select_representatives
from data.universe import UniverseManager
from data.universe_tiers import UniverseTiers
from universe.intelligence_builder import build_universe_intelligence_state
from universe.snapshot_service import _catalog_lookup, build_universe_snapshot_dict, load_universe_selection_config
from universe.universe_tiers import UniverseIntelligenceState


def test_pearson_perfect_positive():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert pearson_correlation(xs, xs) > 0.99


def test_highly_correlated_clustered():
    # Two assets move together + one independent
    a = [100 + i * 0.1 for i in range(30)]
    b = [x + 0.0001 for x in a]
    c = [50 + (i % 3) * 2 for i in range(30)]
    syms = ["A", "B", "C"]
    series = {"A": a, "B": b, "C": c}
    mat, used = correlation_matrix(syms, series, min_overlap=10)
    assert len(used) == 3
    clusters = cluster_by_correlation(used, mat, threshold=0.85)
    flat = [u for cl in clusters for u in cl]
    assert sorted(flat) == [0, 1, 2]
    assert any(len(cl) >= 2 for cl in clusters)


def test_each_cluster_has_representative():
    symbols = ["X", "Y", "Z"]
    clusters = [[0, 1], [2]]
    scores = {"X": 10.0, "Y": 5.0, "Z": 3.0}
    reps = select_representatives(clusters, symbols, scores)
    assert reps[0] == "X"
    assert reps[1] == "Z"


def test_low_liquidity_excluded():
    r = evaluate_eligibility(
        "FOO",
        asset_class="equity",
        rules={"min_adv_usd": 1_000_000, "allowed_asset_classes": ["equity"]},
        adv_usd=Decimal("100"),
    )
    assert r.ok is False


def test_promotion_anomaly():
    c = PromotionCandidate(
        symbol="Z",
        volume_z=4.0,
        price_z=0.1,
        news_shock=False,
        corr_break_z=0.0,
        funding_bps=None,
        spread_div_bps=0.0,
    )
    d = evaluate_promotion(c, {"volume_z_threshold": 3.0, "promotion_ttl_minutes": 60})
    assert d.promote is True


def test_promotion_expires_demotion_logic():
    from universe.promotion_engine import DemotionState

    st = DemotionState()
    assert st.should_demote("A", signal_gone=True, redundancy_score=0.1) is True
    assert st.should_demote("A", signal_gone=False, redundancy_score=0.9) is True


def test_missing_data_safe_correlation_matrix():
    mat, used = correlation_matrix(["A", "B"], {"A": [1, 2]}, min_overlap=10)
    assert used == []


def test_merge_cluster_payload():
    syms = ["A", "B"]
    corr = [[1.0, 0.9], [0.9, 1.0]]
    clusters = [[0, 1]]
    reps = {0: "A"}
    out = merge_cluster_payload(syms, corr, clusters, reps)
    assert len(out) == 1
    assert set(out[0]["members"]) == {"A", "B"}


def test_snapshot_disabled_does_not_crash(monkeypatch):
    import universe.snapshot_service as ss

    monkeypatch.setattr(ss, "load_universe_selection_config", lambda path=None: {"enabled": False})
    payload = build_universe_snapshot_dict(broker_symbol_totals={"ibkr": 100})
    assert payload["enabled"] is False
    assert "funnel" in payload
    assert isinstance(payload["symbols"], list)


def test_snapshot_symbol_rows_include_description(monkeypatch):
    import universe.snapshot_service as ss

    monkeypatch.setattr(ss, "load_universe_selection_config", lambda path=None: {"enabled": False})
    payload = build_universe_snapshot_dict(broker_symbol_totals={})
    assert payload["symbols"]
    for row in payload["symbols"]:
        assert row.get("description")
        assert row.get("sym")


def test_snapshot_enabled_missing_artifact_reports_fallback(monkeypatch, tmp_path):
    import universe.snapshot_service as ss

    monkeypatch.setattr(ss, "load_universe_selection_config", lambda path=None: {"enabled": True, "rebuild": {"interval_sec": 60}})
    payload = build_universe_snapshot_dict(
        broker_symbol_totals={"ibkr": 100},
        intelligence_path=tmp_path / "missing.json",
    )
    assert payload["enabled"] is True
    assert payload["fallback"]
    assert payload["build"]["state"] == "missing"
    assert payload["clusters"] == []


def test_catalog_lookup_resolves_aliases(monkeypatch):
    import universe.snapshot_service as ss

    monkeypatch.setattr(ss, "load_universe_selection_config", lambda path=None: {"enabled": False})
    assert _catalog_lookup("SPY") == ("S&P 500 ETF", "broad_market")
    assert _catalog_lookup("BTC-USD")[0] == "Bitcoin"
    assert _catalog_lookup("EUR.USD")[0] == "Euro/Dollar"
    unknown = _catalog_lookup("ZZZNOTREAL")
    assert unknown == (None, None)
    catalog_syms = {inst.symbol.upper() for inst in UniverseManager.INITIAL_UNIVERSE}
    payload = build_universe_snapshot_dict(broker_symbol_totals={})
    overlap = [s for s in payload["symbols"] if s.get("sym") in catalog_syms]
    if overlap:
        row = overlap[0]
        assert row.get("name")
        assert row["sym"] in catalog_syms


def test_load_config():
    cfg = load_universe_selection_config()
    assert "enabled" in cfg


def test_intelligence_state_roundtrip():
    s = UniverseIntelligenceState(candidate_count=10, cold_scan=["A"], core=["B"])
    s2 = UniverseIntelligenceState.from_json_obj(s.to_json_obj())
    assert s2.cold_scan == ["A"]


def test_intelligence_builder_clusters_without_touching_disk():
    tiers = UniverseTiers(
        core=("AAA", "BBB", "CCC"),
        scan=("DDD", "EEE"),
        light=(),
        scores={"AAA": 10, "BBB": 20, "CCC": 5, "DDD": 3, "EEE": 2},
        updated_at="2026-05-12T18:00:00+00:00",
    )

    def history(sym: str) -> list[float]:
        if sym in {"AAA", "BBB"}:
            return [100 + i for i in range(30)]
        return [100 + ((i * (idx + 1)) % 7) for idx, i in enumerate(range(30))]

    import asyncio

    state = asyncio.run(
        build_universe_intelligence_state(
            tiers,
            cfg={"cluster_max_symbols": 5, "cluster_yf_concurrency": 2, "correlation_cluster_threshold": 0.85},
            history_fetcher=history,
        )
    )
    assert state is not None
    assert state.clusters
    assert any("AAA" in c["members"] and "BBB" in c["members"] for c in state.clusters)
    assert "BBB" in state.core


def test_intelligence_builder_derives_promotions_from_conviction_scores():
    tiers = UniverseTiers(
        core=("AAA",),
        scan=("BBB", "CCC"),
        light=("DDD",),
        scores={"AAA": 1, "BBB": 100, "CCC": 50, "DDD": 0},
        updated_at="2026-05-12T18:00:00+00:00",
    )

    def history(sym: str) -> list[float]:
        offset = {"AAA": 0, "BBB": 5, "CCC": 11, "DDD": 17}.get(sym, 0)
        return [100 + offset + i * (1 + offset / 100) for i in range(30)]

    import asyncio

    state = asyncio.run(
        build_universe_intelligence_state(
            tiers,
            cfg={
                "cluster_max_symbols": 4,
                "cluster_yf_concurrency": 2,
                "min_cluster_price_series": 3,
                "correlation_cluster_threshold": 0.9999,
                "promotion": {"conviction_threshold": 65, "promotion_ttl_minutes": 60},
            },
            history_fetcher=history,
        )
    )
    assert state is not None
    promoted = {p["symbol"]: p for p in state.promotions}
    assert "BBB" in promoted
    assert promoted["BBB"]["reason"] == "conviction_score"
