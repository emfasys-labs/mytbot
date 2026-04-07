from data.scanner import UniverseScanner
from data.universe import UniverseManager


def test_scanner_math_helpers() -> None:
    u = UniverseManager()
    s = UniverseScanner(u, session_factory=lambda: None, cooldown_seconds=0)
    z = s._compute_z_score(10.0, [1.0] * 30)  # noqa: SLF001
    assert z == 0.0
    score = s._compute_anomaly_score(3.0, 2.0, 4.0)  # noqa: SLF001
    assert 0.0 <= score <= 1.0
