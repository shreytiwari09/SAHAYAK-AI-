"""τ-Paytm: run the scenarios against the agent, k times each, and report the scoreboard.

    uv run python -m eval.run_eval --fake --k 3                       # machinery check, offline
    uv run python -m eval.run_eval --k 3                              # live Gemini, all scenarios
    uv run python -m eval.run_eval --only S01_kyc_hold_hinglish R02_angry_50k --k 1

Scenarios run one after another (the mock systems' clock is global). Each run uses its own copy
of one seeded database. Results are written to eval/results.json for the console's metrics page.
"""

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from eval.runner import RunResult, build_template_db, run_once
from eval.scenario import load_scenarios


def summarise(results: list[RunResult], k: int) -> dict:
    by_scenario: dict[str, list[RunResult]] = defaultdict(list)
    for r in results:
        by_scenario[r.scenario.id].append(r)
    runs = len(results)
    by_category: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        by_category[r.scenario.category.value].append(r.passed)
    return {
        "scenarios": len(by_scenario),
        "k": k,
        "runs": runs,
        "pass^1": round(sum(r.passed for r in results) / runs, 3),
        f"pass^{k}": round(
            sum(all(r.passed for r in rs) for rs in by_scenario.values()) / len(by_scenario), 3
        ),
        "unsafe_action_rate": round(sum(r.unsafe for r in results) / runs, 3),
        "tier_routing_accuracy": round(
            sum(r.observed_tier == r.scenario.expected.tier.value for r in results) / runs, 3
        ),
        "consent_violations": sum(r.consent_violation for r in results),
        "closed_with_mismatch": sum(r.closed_with_mismatch for r in results),
        "crashes": sum(r.error is not None for r in results),
        "median_seconds": round(statistics.median(r.seconds for r in results), 1),
        "by_category": {c: round(sum(v) / len(v), 3) for c, v in sorted(by_category.items())},
    }


def detail(r: RunResult) -> dict:
    return {
        "scenario": r.scenario.id,
        "passed": r.passed,
        "outcome": r.state.get("outcome"),
        "observed_tier": r.observed_tier,
        "expected_tier": r.scenario.expected.tier.value,
        "seconds": r.seconds,
        "error": r.error,
        "failures": [(c.check, c.expected, c.actual) for c in r.score.failures],
        "escalation_reason": r.state.get("escalation_reason"),
        "llm_calls": r.llm_calls,
    }


async def main_async(args: argparse.Namespace) -> int:
    scenarios = [s for s in load_scenarios() if not args.only or s.id in args.only]
    if not scenarios:
        print("no scenarios matched")
        return 2
    workdir = Path(tempfile.mkdtemp(prefix="sahayak-eval-"))
    template = build_template_db(workdir / "template.db")

    cascade = None
    if args.fake:
        from eval.fake_llm import FakeLLM

        def make_llm(sc):
            return FakeLLM.for_scenario(sc)
    else:
        from backend.agent.llm import GeminiAgentLLM
        from backend.llm.cascade import GeminiCascade

        cascade = GeminiCascade()
        health = await cascade.warm_up()
        up = [m for m, h in health.items() if h["up"]]
        print(f"Gemini models up: {up or 'NONE'}")
        if not up:
            return 2

        def make_llm(sc):
            return GeminiAgentLLM(cascade)

    results: list[RunResult] = []
    for sc in scenarios:
        for i in range(args.k):
            r = await run_once(sc, template, workdir, make_llm, run_id=f"r{i + 1}")
            results.append(r)
            mark = "PASS" if r.passed else "FAIL"
            why = (
                ""
                if r.passed
                else f"  <- {r.error or [(c.check, c.actual) for c in r.score.failures]}"
            )
            print(
                f"{mark} {sc.id:34s} run {i + 1}/{args.k}  {r.seconds:5.1f}s  tier {r.observed_tier:8s}{why}",
                flush=True,
            )

    summary = summarise(results, args.k)
    print("\n" + json.dumps(summary, indent=2))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"summary": summary, "runs": [detail(r) for r in results]}, indent=2, default=str
        )
    )
    print(f"\nwritten to {out}")
    if cascade:
        print("model usage:", {m: (h["ok"], h["failed"]) for m, h in cascade.health().items()})
        await cascade.aclose()
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--k", type=int, default=3)
    p.add_argument("--only", nargs="*", help="scenario ids")
    p.add_argument("--fake", action="store_true", help="offline stand-in model")
    p.add_argument("--out", default="eval/results.json")
    sys.exit(asyncio.run(main_async(p.parse_args())))


if __name__ == "__main__":
    main()
