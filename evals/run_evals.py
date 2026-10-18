"""Run the 5 tests (or also the extra ones) through the two-agent system and the single agent.

A test passes when matches.csv ends up holding exactly the pair from data/eval/ground_truth.json
for the post the query names, L.. or F.. (or nothing, when no true match exists or the query is free text). For posts that must be refused (expected_refusal),
confirm_match must not be called at all. Only this runner reads data/eval/; agents never do.

matches.csv is cleared before every test and restored to its original content at the end.

Usage:
    python evals/run_evals.py                  # both modes, the 5 main tests
    python evals/run_evals.py --mode multi --extra --model qwen3:8b
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from lostfound.agents.base import log, model_name
from lostfound.agents.orchestrator import run_system
from lostfound.agents.single import run_single
from lostfound.server_v2 import MATCH_FIELDS, MATCHES_PATH
from lostfound.tracing import TRACE_PATH, flush

ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = ROOT / "data" / "eval"
RESULTS_DIR = ROOT / "evals" / "results"
LOST_ID = re.compile(r"\bL\d{2,}\b")
FOUND_ID = re.compile(r"\bF\d{2,}\b")


def load_tests(extra: bool) -> list[dict]:
    spec = json.loads((EVAL_DIR / "tests.json").read_text(encoding="utf-8"))
    tests = [{**t, "kind": "main"} for t in spec["tests"]]
    if extra:
        tests += [
            {"n": f"x{i}", "name": "Додатковий", "kind": "extra", **t}
            for i, t in enumerate(spec.get("extra_tests", []), 1)
        ]
    return tests


def expected_outcome(query: str, truth: dict) -> tuple[set[tuple[str, str]], bool]:
    """(pairs that must be in matches.csv, whether confirm_match must not be called)."""
    lost_match, found_match = LOST_ID.search(query), FOUND_ID.search(query)
    if lost_match:
        lost_id = lost_match.group()
        pairs = {(m["lost"], m["found"]) for m in truth["true_matches"] if m["lost"] == lost_id}
        return pairs, any(r["lost"] == lost_id for r in truth["expected_refusal"])
    if found_match:
        found_id = found_match.group()
        return {(m["lost"], m["found"]) for m in truth["true_matches"] if m["found"] == found_id}, False
    # Free text names no post in the chat, so nothing may be written
    return set(), False


def reset_matches() -> None:
    with MATCHES_PATH.open("w", encoding="utf-8", newline="") as file:
        csv.DictWriter(file, fieldnames=MATCH_FIELDS).writeheader()


def written_pairs() -> list[dict]:
    with MATCHES_PATH.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def confirm_calls(mode: str, result: dict) -> int:
    if mode == "single":
        return result["confirm_attempts"]
    # A verdict other than "declined" means the Verifier called confirm_match
    return sum(1 for a in result["attempts"] if (a.get("verdict") or {}).get("status") in ("confirmed", "rejected"))


def error_codes(mode: str, result: dict) -> list[str]:
    if mode == "single":
        return []
    return [a["verdict"]["error_code"] for a in result["attempts"] if a.get("verdict") and a["verdict"]["error_code"]]


async def run_one(mode: str, test: dict, truth: dict, model: str) -> dict:
    reset_matches()
    started = time.perf_counter()
    try:
        result = await (run_system if mode == "multi" else run_single)(test["query"], model=model)
        crash = None
    except Exception as e:  # a crash is a failed test, not a failed run
        log.exception("test %s crashed", test["n"])
        result, crash = {}, f"{type(e).__name__}: {e}"
    seconds = round(time.perf_counter() - started, 1)

    rows = written_pairs()
    got = {(r["lost_id"], r["found_id"]) for r in rows}
    want, must_refuse = expected_outcome(test["query"], truth)
    calls = confirm_calls(mode, result) if result else 0

    problems = []
    if crash:
        problems.append(f"crash: {crash}")
    if got - want:
        problems.append(f"wrong write: {sorted(got - want)}")
    if want - got:
        problems.append(f"missing write: {sorted(want - got)}")
    if must_refuse and calls:
        problems.append(f"confirm_match called {calls}x on a post that must be refused")

    return {
        "n": test["n"],
        "kind": test["kind"],
        "name": test["name"],
        "query": test["query"],
        "expected": test["expected"],
        "passed": not problems,
        "problems": problems,
        "written": [dict(r) for r in rows],
        "confirm_calls": calls,
        "error_codes": error_codes(mode, result) if result else [],
        "status": result.get("status"),
        "reply": result.get("reply"),
        "seconds": seconds,
        "raw": result,
    }


def summary_markdown(model: str, runs: dict[str, list[dict]]) -> str:
    lines = [f"# Eval run {datetime.now():%Y-%m-%d %H:%M}", "", f"Model: `{model}`", ""]
    modes = list(runs)
    header = "| # | Test | " + " | ".join(modes) + " |"
    lines += [header, "|" + "---|" * (2 + len(modes))]
    tests = runs[modes[0]]
    for i, test in enumerate(tests):
        cells = []
        for mode in modes:
            r = runs[mode][i]
            cells.append("✅" if r["passed"] else "❌ " + "; ".join(r["problems"]))
        lines.append(f"| {test['n']} | {test['name']} | " + " | ".join(cells) + " |")
    lines.append("")
    for mode in modes:
        main = [r for r in runs[mode] if r["kind"] == "main"]
        lines.append(f"- **{mode}**: {sum(r['passed'] for r in main)}/{len(main)} main tests passed")
    lines += ["", f"Trace: `{TRACE_PATH.relative_to(ROOT)}`", ""]
    return "\n".join(lines)


async def main_async(args) -> None:
    truth = json.loads((EVAL_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    tests = load_tests(args.extra)
    modes = ["multi", "single"] if args.mode == "both" else [args.mode]
    model = args.model or model_name()

    original = MATCHES_PATH.read_bytes() if MATCHES_PATH.exists() else None
    runs: dict[str, list[dict]] = {}
    try:
        for mode in modes:
            runs[mode] = []
            for test in tests:
                log.info("\n===== [%s] test %s: %s =====", mode, test["n"], test["query"])
                result = await run_one(mode, test, truth, model)
                log.info("===== %s %s", "PASS" if result["passed"] else "FAIL", "; ".join(result["problems"]))
                runs[mode].append(result)
    finally:
        if original is None:
            MATCHES_PATH.unlink(missing_ok=True)
        else:
            MATCHES_PATH.write_bytes(original)
        flush()

    out = RESULTS_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(
        json.dumps({"model": model, "runs": runs}, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    summary = summary_markdown(model, runs)
    (out / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    print(f"Saved to {out.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the two-agent system against a single agent")
    parser.add_argument("--mode", choices=["multi", "single", "both"], default="both")
    parser.add_argument("--extra", action="store_true", help="also run extra_tests from tests.json")
    parser.add_argument("--model", default=None, help="Ollama model, default from LOSTFOUND_MODEL")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    log.setLevel(logging.INFO)
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
