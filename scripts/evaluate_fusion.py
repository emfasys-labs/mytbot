"""
scripts/evaluate_fusion.py
============================
Wave 7 — read a context CSV and print decomposed fusion scores.

Expected CSV columns (all optional; missing ⇒ source is skipped):

    symbol, asset_class,
    forecast_expected_return, forecast_confidence,
    news_score, news_materiality,
    regime_label, regime_score,
    accumulator_score, accumulator_confidence,
    last_slippage_bps,
    graph_propagation_strength, graph_upstream_trigger
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai.fusion import FusionConfig, MultimodalFusion  # noqa: E402
from ai.market_context import GraphContext, MarketContextBuilder  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Evaluate multimodal fusion (Wave 7)")
    p.add_argument("--context", required=True, help="CSV with the columns documented in this file")
    p.add_argument("--config", default="config/multimodal_fusion.yaml")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    cfg = FusionConfig.load(args.config)
    cfg.enabled = True  # research-only override

    df = pd.read_csv(args.context)
    fusion = MultimodalFusion(cfg)

    for _, row in df.iterrows():
        symbol = str(row.get("symbol", ""))
        if not symbol:
            continue

        forecast = None
        if "forecast_expected_return" in row and pd.notna(row.get("forecast_expected_return")):
            forecast = SimpleNamespace(
                used=True,
                expected_return=float(row["forecast_expected_return"]),
                expected_volatility=None,
                confidence=float(row["forecast_confidence"]) if pd.notna(row.get("forecast_confidence")) else None,
                horizons_used=(),
            )

        graph_ctx = None
        if pd.notna(row.get("graph_propagation_strength")):
            graph_ctx = GraphContext(
                propagation_strength=float(row["graph_propagation_strength"]),
                upstream_trigger=str(row.get("graph_upstream_trigger") or "") or None,
            )

        ctx = MarketContextBuilder.from_inputs(
            symbol=symbol,
            asset_class=str(row.get("asset_class", "other")),
            forecast_decision=forecast,
            news_score=float(row["news_score"]) if pd.notna(row.get("news_score")) else None,
            news_materiality=float(row["news_materiality"]) if pd.notna(row.get("news_materiality")) else None,
            regime_label=str(row.get("regime_label") or "") or None,
            regime_score=float(row["regime_score"]) if pd.notna(row.get("regime_score")) else None,
            accumulator_net=(
                SimpleNamespace(
                    score=float(row["accumulator_score"]),
                    confidence=float(row["accumulator_confidence"])
                    if pd.notna(row.get("accumulator_confidence"))
                    else 0.5,
                    aligned_sources=(),
                    conflicting_sources=(),
                )
                if pd.notna(row.get("accumulator_score"))
                else None
            ),
            last_slippage_bps=float(row["last_slippage_bps"]) if pd.notna(row.get("last_slippage_bps")) else None,
            graph_context=graph_ctx,
        )
        score = fusion.combine(ctx)
        print(
            f"{symbol:<10}  bias={score.directional_bias:+.3f}  "
            f"conf={score.confidence:.2f}  conflict={score.conflict_score:.2f}  "
            f"trigger_llm={score.trigger_llm_ensemble}  rationale={score.rationale}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
