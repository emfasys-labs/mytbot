from data.universe import UniverseInstrument, UniverseManager


def test_universe_basic_filters_and_add() -> None:
    u = UniverseManager()
    assert len(u) > 40
    assert any(x.symbol == "SPY" for x in u.get_triggers())
    assert len(u.get_by_asset_class("crypto")) >= 3
    n0 = len(u)
    u.add_instrument(
        UniverseInstrument(
            symbol="TEST1",
            name="Test One",
            asset_class="equity",
            broker="ibkr",
            broker_symbol="TEST1",
            sector="test",
            region="US",
        ),
        reason="unit_test",
    )
    assert len(u) == n0 + 1
