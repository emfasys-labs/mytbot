from ai.news_classifier import NewsClassifier, NewsItem


def test_parse_response_clamps_and_normalizes_values():
    c = NewsClassifier(api_key="x")
    item = NewsItem(headline="h", source="s", published_at="2026-01-01T00:00:00+00:00")
    raw = """
    {
      "sentiment": 9.9,
      "confidence": -1.0,
      "affected_symbols": [" spy ", "BTC", "SPY"],
      "event_type": "unknown_type",
      "directional_bias": "up_only",
      "rationale": "test rationale",
      "decay_hours": 999
    }
    """
    s = c._parse_response(item, raw)
    assert s.sentiment == 1.0
    assert s.confidence == 0.0
    assert s.affected_symbols == ["BTC", "SPY"]
    assert s.event_type == "other"
    assert s.directional_bias == "neutral"
    assert s.decay_hours == 168


def test_extract_json_from_wrapped_text():
    c = NewsClassifier(api_key="x")
    wrapped = 'result:\n```json\n{\n  "sentiment": 0.2\n}\n```'
    out = c._extract_json(wrapped)
    assert out["sentiment"] == 0.2
