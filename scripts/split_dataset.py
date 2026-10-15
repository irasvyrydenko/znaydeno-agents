"""Split data/processed/lostfound_dataset.json into the files the project uses.

Agent-visible (read by the MCP server):
    data/processed/lost.csv
    data/processed/found.csv
    data/processed/matches.csv      header only; written by confirm_match

Eval-only (must never be exposed to agents or tools):
    data/eval/ground_truth.json
    data/eval/tests.json            the 5 main tests + extra tests

Usage:
    python scripts/split_dataset.py [--force]

matches.csv is not overwritten if it already has rows, unless --force is given.
"""
import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data" / "processed" / "lostfound_dataset.json"
PROCESSED = ROOT / "data" / "processed"
EVAL = ROOT / "data" / "eval"

POST_FIELDS = ["id", "posted_at", "event_date", "author", "status", "text"]
MATCH_FIELDS = ["lost_id", "found_id", "reason", "confirmed_at"]


def write_posts(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=POST_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            # null event_date becomes an empty string
            writer.writerow({k: ("" if row.get(k) is None else row[k]) for k in POST_FIELDS})


def write_json(path: Path, data) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def validate(data: dict) -> None:
    lost_ids = {r["id"] for r in data["lost"]}
    found_ids = {r["id"] for r in data["found"]}
    if len(lost_ids) != len(data["lost"]) or len(found_ids) != len(data["found"]):
        sys.exit("Duplicate ids in lost or found")

    gt = data["ground_truth"]
    for m in gt["true_matches"]:
        if m["lost"] not in lost_ids or m["found"] not in found_ids:
            sys.exit(f"Unknown id in true_matches: {m}")
    for key in ("expected_weak_match", "expected_no_match", "expected_refusal"):
        for item in gt.get(key, []):
            if item["lost"] not in lost_ids:
                sys.exit(f"Unknown lost id in {key}: {item}")
    for fid in gt.get("unmatched_found", []):
        if fid not in found_ids:
            sys.exit(f"Unknown found id in unmatched_found: {fid}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--force", action="store_true", help="overwrite non-empty matches.csv")
    args = parser.parse_args()

    with SOURCE.open(encoding="utf-8") as f:
        data = json.load(f)
    validate(data)

    EVAL.mkdir(parents=True, exist_ok=True)

    write_posts(PROCESSED / "lost.csv", data["lost"])
    write_posts(PROCESSED / "found.csv", data["found"])

    matches = PROCESSED / "matches.csv"
    has_rows = matches.exists() and len(matches.read_text(encoding="utf-8").splitlines()) > 1
    if has_rows and not args.force:
        print("matches.csv has recorded matches, left untouched (use --force to reset)")
    else:
        with matches.open("w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow(MATCH_FIELDS)

    write_json(EVAL / "ground_truth.json", data["ground_truth"])
    write_json(EVAL / "tests.json", {"tests": data["tests"], "extra_tests": data.get("extra_tests", [])})

    print(f"lost.csv: {len(data['lost'])} rows, found.csv: {len(data['found'])} rows")
    print(f"ground_truth.json: {len(data['ground_truth']['true_matches'])} true matches")
    print(f"tests.json: {len(data['tests'])} tests + {len(data.get('extra_tests', []))} extra")


if __name__ == "__main__":
    main()
