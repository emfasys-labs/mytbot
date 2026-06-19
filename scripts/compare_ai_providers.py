"""
scripts/compare_ai_providers.py
===============================
Quality A/B harness for the news-reasoning layer.

Scores an identical, curated benchmark of financial headlines through the
**local** reasoning model (whatever `config/ai.yaml::providers.local_reasoning`
points at, e.g. Ollama `gpt-oss:20b`) and one or more **hosted** OpenAI-compatible
candidates (Gemini Flash, Groq, OpenRouter, ...), then reports how closely each
candidate agrees with the local baseline plus latency/JSON-validity.

The whole point is: prove we are NOT losing reasoning quality before switching
the live `local_reasoning` provider off the local GPU and onto a free hosted API.

Read-only. Makes no DB writes and changes no live config. It only sends the
benchmark headlines to each endpoint and prints/saves a comparison report.

Usage (PowerShell):
    # local baseline only (no key needed; Ollama must be running)
    python scripts/compare_ai_providers.py --local-only

    # local vs Gemini Flash (free key from https://aistudio.google.com/apikey)
    $env:GEMINI_API_KEY="..."; python scripts/compare_ai_providers.py --gemini

    # local vs Gemini vs Groq
    $env:GEMINI_API_KEY="..."; $env:GROQ_API_KEY="..."; \
        python scripts/compare_ai_providers.py --gemini --groq

Options:
    --gemini / --groq / --openrouter   add that hosted candidate
    --gemini-model / --groq-model / --openrouter-model   override model id
    --local-only                       score only the local baseline
    --no-local                         skip local (compare hosted vs hosted)
    --json PATH                        write the full report JSON to PATH
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Windows consoles default to cp1252; force UTF-8 so report glyphs never crash.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

# Make the repo root importable when run as `python scripts/compare_ai_providers.py`.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

load_dotenv(_REPO_ROOT / ".env")

from ai.providers.local_reasoning_provider import LocalReasoningProvider  # noqa: E402
from ai.schemas import ProviderResult  # noqa: E402


# ── Curated benchmark: representative financial headlines across event types ──
# Each item carries the analyst-expected directional bias purely as a sanity
# anchor (the harness primarily measures *agreement vs the local baseline*, not
# accuracy against a label — neither model is ground truth).
BENCHMARK: list[dict[str, Any]] = [
    {
        "headline": "Apple beats Q3 earnings expectations, raises full-year guidance on strong iPhone demand",
        "source": "Reuters", "expected_bias": "bullish", "event_type": "earnings",
    },
    {
        "headline": "Federal Reserve raises interest rates by 50 basis points, signals more hikes ahead",
        "source": "Bloomberg", "expected_bias": "bearish", "event_type": "macro",
    },
    {
        "headline": "US CPI inflation cools to 3.1%, below economist forecasts",
        "source": "CNBC", "expected_bias": "bullish", "event_type": "macro",
    },
    {
        "headline": "SEC approves spot Bitcoin ETF applications from BlackRock and Fidelity",
        "source": "CoinDesk", "expected_bias": "bullish", "event_type": "crypto",
    },
    {
        "headline": "Major crypto exchange halts withdrawals citing liquidity concerns",
        "source": "The Block", "expected_bias": "bearish", "event_type": "crypto",
    },
    {
        "headline": "Nvidia announces $50 billion share buyback and 10-for-1 stock split",
        "source": "WSJ", "expected_bias": "bullish", "event_type": "company",
    },
    {
        "headline": "Boeing shares slump after regulator grounds 737 fleet over safety probe",
        "source": "Financial Times", "expected_bias": "bearish", "event_type": "regulatory",
    },
    {
        "headline": "Microsoft to acquire cybersecurity firm in $20 billion all-cash deal",
        "source": "Reuters", "expected_bias": "bullish", "event_type": "mna",
    },
    {
        "headline": "Oil prices spike as conflict escalates in the Middle East, shipping routes threatened",
        "source": "Bloomberg", "expected_bias": "bearish", "event_type": "geopolitical",
    },
    {
        "headline": "Tesla misses delivery targets for third straight quarter amid weakening demand",
        "source": "CNBC", "expected_bias": "bearish", "event_type": "earnings",
    },
    {
        "headline": "UK GDP unexpectedly contracts 0.3% as cost-of-living crisis bites",
        "source": "Financial Times", "expected_bias": "bearish", "event_type": "macro",
    },
    {
        "headline": "Amazon Web Services reports record cloud revenue, margins expand",
        "source": "WSJ", "expected_bias": "bullish", "event_type": "earnings",
    },
    {
        "headline": "Company announces routine quarterly dividend in line with prior periods",
        "source": "PR Newswire", "expected_bias": "neutral", "event_type": "company",
    },
    {
        "headline": "Gold hits record high as investors flee to safe havens amid banking turmoil",
        "source": "Reuters", "expected_bias": "bullish", "event_type": "macro",
    },
    {
        "headline": "Pfizer drug fails late-stage trial, shares tumble in pre-market trading",
        "source": "CNBC", "expected_bias": "bearish", "event_type": "company",
    },
]


# ── Hosted endpoint presets (OpenAI-compatible) ──────────────────────────────
HOSTED_PRESETS: dict[str, dict[str, str]] = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
        "key_env": "GEMINI_API_KEY",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "key_env": "GROQ_API_KEY",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "key_env": "OPENROUTER_API_KEY",
    },
}


def _load_local_cfg() -> dict[str, Any]:
    """Read providers.local_reasoning from config/ai.yaml (the live local model)."""
    ai_yaml = _REPO_ROOT / "config" / "ai.yaml"
    if not ai_yaml.exists():
        return {}
    data = yaml.safe_load(ai_yaml.read_text(encoding="utf-8")) or {}
    return dict(((data.get("providers") or {}).get("local_reasoning") or {}))


def _ollama_local_cfg(model: str = "gpt-oss:20b") -> dict[str, Any]:
    """Explicit Ollama baseline for A/B runs (independent of live ai.yaml)."""
    return {
        "provider": "ollama",
        "base_url": "http://localhost:11434",
        "model_name": model,
        "fallback_model": None,
        "temperature": 0.1,
        "max_tokens": 900,
        "timeout_seconds": 180.0,
        "failure_cooldown_seconds": 0,
        "gpu_concurrency": 1,
        "use_json_mode": True,
    }


def _make_local_provider(*, use_ollama: bool = False, ollama_model: str = "gpt-oss:20b") -> LocalReasoningProvider:
    if use_ollama:
        cfg = _ollama_local_cfg(ollama_model)
    else:
        cfg = _load_local_cfg()
    # Give the local model a very generous timeout for this offline benchmark so
    # a heavy 20B reasoning model on a busy single GPU is not unfairly recorded
    # as a timeout failure (live config uses a fast-fail 20s; here we want it to
    # actually answer so the quality comparison is fair).
    cfg = {**cfg, "timeout_seconds": 180.0, "failure_cooldown_seconds": 0}
    return LocalReasoningProvider(cfg)


def _make_hosted_provider(preset_name: str, model_override: str | None) -> tuple[LocalReasoningProvider, str]:
    preset = HOSTED_PRESETS[preset_name]
    key = os.getenv(preset["key_env"], "").strip()
    if not key:
        raise RuntimeError(
            f"{preset_name}: no API key. Set ${preset['key_env']} "
            f"(free key: {'aistudio.google.com/apikey' if preset_name == 'gemini' else preset['base_url']})."
        )
    model = model_override or preset["model"]
    cfg = {
        "base_url": preset["base_url"],
        "model_name": model,
        "api_key": key,
        "timeout_seconds": 30,
        # Gemini 2.5 is a *thinking* model: reasoning tokens are drawn from the
        # output budget, so a tight max_tokens can starve the visible JSON. Give
        # ample headroom so the structured answer always lands.
        "max_tokens": 2048,
        "temperature": 0.1,
        "use_json_mode": True,
        "failure_cooldown_seconds": 0,
    }
    return LocalReasoningProvider(cfg), model


async def _score_all(provider: LocalReasoningProvider) -> list[ProviderResult]:
    ok = await provider.startup_check()
    if not ok:
        return [ProviderResult(provider_name="unavailable", success=False, error="startup_failed")
                for _ in BENCHMARK]
    now = datetime.now(timezone.utc).isoformat()
    results: list[ProviderResult] = []
    for item in BENCHMARK:
        r = await provider.score_headline(item["headline"], None, item["source"], now)
        results.append(r)
    return results


def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a or []), set(b or [])
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _agreement(baseline: list[ProviderResult], cand: list[ProviderResult]) -> dict[str, Any]:
    bias_hits = ev_hits = paired = 0
    sent_deltas: list[float] = []
    conf_deltas: list[float] = []
    jaccards: list[float] = []
    for b, c in zip(baseline, cand):
        if not (b.success and c.success):
            continue
        paired += 1
        if (b.directional_bias or "") == (c.directional_bias or ""):
            bias_hits += 1
        if (b.event_type or "") == (c.event_type or ""):
            ev_hits += 1
        sent_deltas.append(abs((b.sentiment or 0.0) - (c.sentiment or 0.0)))
        conf_deltas.append(abs((b.confidence or 0.0) - (c.confidence or 0.0)))
        jaccards.append(_jaccard(b.affected_symbols, c.affected_symbols))
    return {
        "paired": paired,
        "bias_agreement": (bias_hits / paired) if paired else 0.0,
        "event_agreement": (ev_hits / paired) if paired else 0.0,
        "sentiment_mae": (statistics.fmean(sent_deltas) if sent_deltas else 0.0),
        "confidence_mae": (statistics.fmean(conf_deltas) if conf_deltas else 0.0),
        "symbol_jaccard": (statistics.fmean(jaccards) if jaccards else 0.0),
    }


def _provider_summary(results: list[ProviderResult]) -> dict[str, Any]:
    ok = [r for r in results if r.success]
    lat = [r.latency_ms for r in ok if r.latency_ms]
    return {
        "json_ok_rate": (len(ok) / len(results)) if results else 0.0,
        "n_ok": len(ok),
        "n_total": len(results),
        "latency_ms_mean": (statistics.fmean(lat) if lat else 0.0),
        "latency_ms_median": (statistics.median(lat) if lat else 0.0),
    }


def _bias_vs_expected(results: list[ProviderResult]) -> float:
    """How often the model's bias matches the curated analyst-expected bias."""
    hits = n = 0
    for item, r in zip(BENCHMARK, results):
        if not r.success:
            continue
        n += 1
        if (r.directional_bias or "") == item["expected_bias"]:
            hits += 1
    return (hits / n) if n else 0.0


def _verdict(agree: dict[str, Any]) -> tuple[str, list[str]]:
    """Heuristic go/no-go for switching, with the reasons behind it."""
    reasons: list[str] = []
    ok = True
    if agree["paired"] < max(8, int(0.7 * len(BENCHMARK))):
        ok = False
        reasons.append(f"only {agree['paired']} comparable items (need a full clean run)")
    if agree["bias_agreement"] < 0.80:
        ok = False
        reasons.append(f"directional-bias agreement {agree['bias_agreement']:.0%} < 80%")
    if agree["sentiment_mae"] > 0.25:
        ok = False
        reasons.append(f"sentiment MAE {agree['sentiment_mae']:.2f} > 0.25")
    if agree["event_agreement"] < 0.70:
        reasons.append(f"event-type agreement {agree['event_agreement']:.0%} < 70% (soft)")
    verdict = "QUALITY MATCH — safe to switch" if ok else "DIVERGENCE — review before switching"
    return verdict, reasons


def _fmt(r: ProviderResult) -> str:
    if not r.success:
        return f"FAIL({r.error})"
    return (f"{r.directional_bias:<7} s={r.sentiment:+.2f} c={r.confidence:.2f} "
            f"{(r.event_type or ''):<12} {r.latency_ms:>5}ms")


def _headline_wins_vs_expected(results: list[ProviderResult]) -> list[tuple[int, str, str, str]]:
    """Rows where model bias matched curated expected_bias."""
    wins: list[tuple[int, str, str, str]] = []
    for i, (item, r) in enumerate(zip(BENCHMARK, results)):
        if r.success and (r.directional_bias or "") == item["expected_bias"]:
            wins.append((i + 1, item["headline"][:60], item["expected_bias"], r.directional_bias or ""))
    return wins


def _disagreement_scorecard(
    a_label: str, a_results: list[ProviderResult],
    b_label: str, b_results: list[ProviderResult],
) -> dict[str, Any]:
    """On headlines where the two models disagree on bias, who matches expected?"""
    a_wins = b_wins = ties = 0
    rows: list[dict[str, Any]] = []
    for item, ar, br in zip(BENCHMARK, a_results, b_results):
        if not (ar.success and br.success):
            continue
        if (ar.directional_bias or "") == (br.directional_bias or ""):
            continue
        exp = item["expected_bias"]
        a_ok = (ar.directional_bias or "") == exp
        b_ok = (br.directional_bias or "") == exp
        if a_ok and not b_ok:
            a_wins += 1
            winner = a_label
        elif b_ok and not a_ok:
            b_wins += 1
            winner = b_label
        else:
            ties += 1
            winner = "both_wrong"
        rows.append({
            "headline": item["headline"][:80],
            "expected": exp,
            a_label: ar.directional_bias,
            b_label: br.directional_bias,
            "winner": winner,
        })
    return {"a_wins": a_wins, "b_wins": b_wins, "ties": ties, "rows": rows}


def _recommend_gemini_flash(
    s25: dict[str, Any], s35: dict[str, Any], agree: dict[str, Any], tiebreak: dict[str, Any],
) -> tuple[str, list[str]]:
    """Pick gemini-2.5-flash vs gemini-3.5-flash for production news scoring."""
    reasons: list[str] = []
    acc25 = float(s25.get("bias_vs_expected", 0))
    acc35 = float(s35.get("bias_vs_expected", 0))
    lat25 = float(s25.get("latency_ms_median", 0))
    lat35 = float(s35.get("latency_ms_median", 0))
    json25 = float(s25.get("json_ok_rate", 0))
    json35 = float(s35.get("json_ok_rate", 0))

    if json35 < 1.0 and json25 >= json35:
        return "gemini-2.5-flash", ["3.5 JSON validity below 2.5"]
    if json25 < 1.0 and json35 > json25:
        return "gemini-3.5-flash", ["2.5 JSON validity below 3.5"]

    if acc35 > acc25 + 0.05:
        return "gemini-3.5-flash", [f"3.5 accuracy {acc35:.0%} vs 2.5 {acc25:.0%} (>5pp lead)"]
    if acc25 > acc35 + 0.05:
        return "gemini-2.5-flash", [f"2.5 accuracy {acc25:.0%} vs 3.5 {acc35:.0%} (>5pp lead)"]

    if tiebreak["b_wins"] > tiebreak["a_wins"] + 0:
        return "gemini-3.5-flash", [
            f"tie on headline accuracy ({acc25:.0%}); 3.5 won {tiebreak['b_wins']} disagreements vs 2.5's {tiebreak['a_wins']}",
        ]
    if tiebreak["a_wins"] > tiebreak["b_wins"]:
        return "gemini-2.5-flash", [
            f"tie on headline accuracy ({acc25:.0%}); 2.5 won {tiebreak['a_wins']} disagreements vs 3.5's {tiebreak['b_wins']}",
        ]

    if lat25 > 0 and lat35 > lat25 * 1.25:
        return "gemini-2.5-flash", [
            f"accuracy tied ({acc25:.0%}); 2.5 median latency {lat25:.0f}ms vs 3.5 {lat35:.0f}ms",
            "2.5 is cheaper if billing is ever enabled ($0.30/$2.50 vs $1.50/$9.00 per 1M tokens)",
        ]
    if lat35 > 0 and lat25 > lat35 * 1.25:
        return "gemini-3.5-flash", [
            f"accuracy tied ({acc25:.0%}); 3.5 median latency {lat35:.0f}ms vs 2.5 {lat25:.0f}ms",
        ]

    return "gemini-2.5-flash", [
        f"accuracy tied ({acc25:.0%}), latency comparable ({lat25:.0f}ms vs {lat35:.0f}ms)",
        "keep 2.5: already live-proven + much cheaper on paid tier",
    ]


async def main() -> int:
    ap = argparse.ArgumentParser(description="Compare local vs hosted AI reasoning quality")
    ap.add_argument("--gemini", action="store_true", help="add Gemini Flash candidate")
    ap.add_argument("--groq", action="store_true", help="add Groq candidate")
    ap.add_argument("--openrouter", action="store_true", help="add OpenRouter candidate")
    ap.add_argument("--gemini-model", default=None)
    ap.add_argument("--groq-model", default=None)
    ap.add_argument("--openrouter-model", default=None)
    ap.add_argument("--local-only", action="store_true", help="score only the local baseline")
    ap.add_argument("--local-ollama", action="store_true",
                    help="baseline = Ollama gpt-oss:20b (not live ai.yaml; use for fair GPU A/B)")
    ap.add_argument("--local-ollama-model", default="gpt-oss:20b",
                    help="Ollama model id when --local-ollama is set")
    ap.add_argument("--no-local", action="store_true", help="skip the local baseline")
    ap.add_argument("--gemini-flash-ab", action="store_true",
                    help="compare gemini-2.5-flash vs gemini-3.5-flash (implies --no-local)")
    ap.add_argument("--json", default=None, help="write the full report JSON to this path")
    args = ap.parse_args()

    if args.gemini_flash_ab:
        args.no_local = True

    candidates: list[tuple[str, LocalReasoningProvider]] = []
    if not args.no_local:
        if args.local_ollama:
            model = args.local_ollama_model
            candidates.append((f"ollama({model})", _make_local_provider(use_ollama=True, ollama_model=model)))
        else:
            candidates.append(("local", _make_local_provider()))
    if args.gemini_flash_ab:
        for model in ("gemini-2.5-flash", "gemini-3.5-flash"):
            try:
                prov, _ = _make_hosted_provider("gemini", model)
                candidates.append((f"gemini({model})", prov))
            except RuntimeError as exc:
                print(f"  ! skipping gemini({model}): {exc}")
    for flag, name, override in (
        (args.gemini and not args.gemini_flash_ab, "gemini", args.gemini_model),
        (args.groq, "groq", args.groq_model),
        (args.openrouter, "openrouter", args.openrouter_model),
    ):
        if flag and not args.local_only:
            try:
                prov, model = _make_hosted_provider(name, override)
                candidates.append((f"{name}({model})", prov))
            except RuntimeError as exc:
                print(f"  ! skipping {name}: {exc}")

    if not candidates:
        print("Nothing to run. Use --gemini/--groq (with a key) or --local-only.")
        return 2

    print(f"\nScoring {len(BENCHMARK)} benchmark headlines through {len(candidates)} provider(s)...\n")
    scored: dict[str, list[ProviderResult]] = {}
    for label, prov in candidates:
        print(f"  -> {label} ...", flush=True)
        scored[label] = await _score_all(prov)

    # Per-headline side-by-side
    print("\n" + "=" * 100)
    print("PER-HEADLINE RESULTS")
    print("=" * 100)
    labels = list(scored.keys())
    for i, item in enumerate(BENCHMARK):
        print(f"\n[{i+1:>2}] {item['headline'][:88]}")
        print(f"     expected: {item['expected_bias']}")
        for label in labels:
            print(f"     {label:<24} {_fmt(scored[label][i])}")

    # Summaries
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "benchmark_size": len(BENCHMARK),
        "providers": {},
        "comparisons": {},
    }
    print("\n" + "=" * 100)
    print("PROVIDER SUMMARY")
    print("=" * 100)
    for label in labels:
        s = _provider_summary(scored[label])
        s["bias_vs_expected"] = _bias_vs_expected(scored[label])
        report["providers"][label] = s
        print(f"  {label:<24} json_ok={s['json_ok_rate']:.0%} ({s['n_ok']}/{s['n_total']})  "
              f"lat_median={s['latency_ms_median']:.0f}ms  "
              f"bias_vs_expected={s['bias_vs_expected']:.0%}")

    # Agreement vs local baseline
    baseline_key = next((k for k in scored if k.startswith("ollama(") or k == "local"), None)
    if baseline_key:
        base = scored[baseline_key]
        print("\n" + "=" * 100)
        print(f"AGREEMENT vs {baseline_key.upper()} BASELINE")
        print("=" * 100)
        for label in labels:
            if label == baseline_key:
                continue
            agree = _agreement(base, scored[label])
            verdict, reasons = _verdict(agree)
            report["comparisons"][label] = {**agree, "verdict": verdict, "reasons": reasons}
            print(f"\n  {label} vs local:")
            print(f"    bias agreement   : {agree['bias_agreement']:.0%}")
            print(f"    event agreement  : {agree['event_agreement']:.0%}")
            print(f"    sentiment MAE    : {agree['sentiment_mae']:.3f}  (0 = identical)")
            print(f"    confidence MAE   : {agree['confidence_mae']:.3f}")
            print(f"    symbol overlap   : {agree['symbol_jaccard']:.0%} (Jaccard)")
            print(f"    >>> VERDICT: {verdict}")
            for r in reasons:
                print(f"        - {r}")

    # Gemini 2.5 vs 3.5 pick
    k25, k35 = "gemini(gemini-2.5-flash)", "gemini(gemini-3.5-flash)"
    if k25 in scored and k35 in scored:
        agree_35_vs_25 = _agreement(scored[k25], scored[k35])
        tiebreak = _disagreement_scorecard(
            "2.5", scored[k25], "3.5", scored[k35],
        )
        pick, pick_reasons = _recommend_gemini_flash(
            report["providers"][k25], report["providers"][k35], agree_35_vs_25, tiebreak,
        )
        report["gemini_flash_recommendation"] = {
            "model": pick,
            "reasons": pick_reasons,
            "agreement_3_5_vs_2_5": agree_35_vs_25,
            "disagreement_tiebreak": tiebreak,
        }
        print("\n" + "=" * 100)
        print("GEMINI FLASH RECOMMENDATION")
        print("=" * 100)
        print(f"  >>> USE: {pick}")
        for r in pick_reasons:
            print(f"      - {r}")
        if tiebreak["rows"]:
            print(f"\n  Disagreements ({len(tiebreak['rows'])}):")
            for row in tiebreak["rows"]:
                print(f"    [{row['winner']}] {row['headline']}")
                print(f"         expected={row['expected']}  2.5={row['2.5']}  3.5={row['3.5']}")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nReport written to {out}")
    else:
        default_out = _REPO_ROOT / "reports" / "ai_provider_comparison" / (
            f"compare_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        )
        default_out.parent.mkdir(parents=True, exist_ok=True)
        default_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nReport written to {default_out}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
