#!/usr/bin/env python3
"""
scripts/report_phase_f_fusion_shadow.py
=========================================
Phase F evidence gate. Read-only. Measures whether the learned
regime-conditional fusion weights rank opportunities better than the
static equal blend, on REAL forward returns from feature_snapshots.

It is the ONLY thing allowed to flip the artifact's
``promote_eligible`` flag — and only when learned IC beats static IC by
a margin. Otherwise the verdict is ``do_not_promote`` and the weights
stay inert/shadow. Mirrors the Phase E evidence report exactly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv()

from system.fusion_weights import (  # noqa: E402
    COMPONENTS,
    DEFAULT_ARTIFACT,
    RegimeConditionalFusionWeights,
)

# Reuse the exact proxy/regime construction from the builder so shadow
# evaluation matches what was learned.
from scripts.build_phase_f_fusion_weights import (  # noqa: E402
    _components_and_target,
    _load,
)

_REPORT_DIR = Path("reports/models/phase_f_fusion_weights")
PROMOTE_MARGIN = 0.005   # learned IC must beat static IC by ≥ this, AND…
MIN_USEFUL_IC = 0.02     # …learned IC must itself be genuinely predictive.
# Without the absolute floor the gate would promote NOISE whenever the
# static baseline is merely anti-correlated (learned IC ≈ 0 "beats" a
# negative static IC). Real signal, not "less bad than broken", is required.


def _ic(score: np.ndarray, fwd: np.ndarray) -> float:
    s, f = np.asarray(score, float), np.asarray(fwd, float)
    mask = np.isfinite(s) & np.isfinite(f)  # drop messy real-data NaN/inf pairs
    s, f = s[mask], f[mask]
    if s.size < 30:
        return float("nan")
    if np.std(s) < 1e-12 or np.std(f) < 1e-12:
        return 0.0
    c = float(np.corrcoef(s, f)[0, 1])
    return c if np.isfinite(c) else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact", default=str(DEFAULT_ARTIFACT))
    ap.add_argument("--promote", action="store_true",
                    help="flip promote_eligible=True IF learned beats static")
    args = ap.parse_args()

    art = RegimeConditionalFusionWeights.load(args.artifact, require_promote=False)
    if art is None:
        raise SystemExit(f"no artifact at {args.artifact} — run build_phase_f_fusion_weights first")

    data = asyncio.run(_load())
    learned_scores, static_scores, fwds = [], [], []
    for _key, series in data.items():
        C, y, lab = _components_and_target(series)
        if C.size == 0:
            continue
        for i in range(C.shape[0]):
            comp = {c: float(C[i, j]) for j, c in enumerate(COMPONENTS)}
            ls = art.shadow_score(comp, str(lab[i]))
            if ls is None:
                continue
            learned_scores.append(ls)
            static_scores.append(float(np.mean(list(comp.values()))))  # equal-weight blend
            fwds.append(float(y[i]))

    n = len(fwds)
    learned_ic = _ic(np.array(learned_scores), np.array(fwds)) if n else float("nan")
    static_ic = _ic(np.array(static_scores), np.array(fwds)) if n else float("nan")
    diff = (learned_ic - static_ic) if (n and not np.isnan(learned_ic)) else float("nan")
    promote = bool(
        n >= 200
        and not np.isnan(diff)
        and diff >= PROMOTE_MARGIN
        and not np.isnan(learned_ic)
        and learned_ic >= MIN_USEFUL_IC  # must be genuinely predictive
    )

    report = {
        "observations": n,
        "learned_ic": round(learned_ic, 6) if n else None,
        "static_ic": round(static_ic, 6) if n else None,
        "learned_minus_static": round(diff, 6) if n and not np.isnan(diff) else None,
        "promote_margin": PROMOTE_MARGIN,
        "min_useful_ic": MIN_USEFUL_IC,
        "recommendation": "promote" if promote else "do_not_promote",
        "regimes": art.metadata.get("regimes"),
    }
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (_REPORT_DIR / "latest_phase_f_fusion_shadow.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    for k, v in report.items():
        print(f"phase_f | {k}: {v}")

    if args.promote and promote:
        art.metadata["promote_eligible"] = True
        art.metadata["promoted_by"] = "report_phase_f_fusion_shadow"
        art.save(args.artifact)
        print("phase_f | promote_eligible flipped TRUE (learned beat static)")
    elif args.promote:
        print("phase_f | NOT promoted — learned did not beat static by margin")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
