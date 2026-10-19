"""Two-agent system: Matcher proposes, Verifier records. Plain code, not an LLM, runs the loop.

Works in both directions: a lost item looks for found posts, a found item for lost posts.
The Verifier always gets the same {lost_id, found_id, reason}, whichever side was searched.

    request -> Matcher -> {lost_id, found_id, reason} -> Verifier -> confirm_match
                 ^                                          |
                 +------------ error + hint ----------------+   (at most MAX_ATTEMPTS)

Usage:
    python -m lostfound.agents.orchestrator "Допоможи знайти L03"
    python -m lostfound.agents.orchestrator "Чиє це? F07"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from lostfound.agents.base import log
from lostfound.agents.matcher import run_matcher
from lostfound.agents.posts import ingest, pair_posts
from lostfound.agents.verifier import run_verifier
from lostfound.tracing import flush, record, traced

MAX_ATTEMPTS = 3


def _needs_details(item: dict) -> bool:
    """A refusal that more details from the owner would fix."""
    return item.get("error_code") == "WEAK_MATCH" or item.get("problem") == "too_vague"


def _sides(handoff: dict) -> tuple[str | None, str | None]:
    """(candidate the Matcher searched for, the post the request is about)."""
    if handoff.get("direction") == "found":
        return handoff["lost_id"], handoff["found_id"]
    return handoff["found_id"], handoff["lost_id"]


def _reply(status: str, attempts: list[dict], match: dict | None) -> str:
    """The answer to the person, in Ukrainian, built from what actually happened."""
    last = attempts[-1] if attempts else {}
    verdict = last.get("verdict") or {}
    handoff = last.get("handoff") or {}
    if status == "confirmed":
        return f"Знайдено! Збіг {match['lost_id']} — {match['found_id']} записано. {verdict.get('message', '')}".strip()
    if status == "candidate_only":
        candidate, _ = _sides(handoff)
        needed = "знахідку (id F..)" if handoff.get("direction") == "found" else "втрату (id L..)"
        return (
            f"Можливий кандидат: {candidate}. {handoff.get('reason', '')} "
            f"Щоб записати збіг, потрібне оголошення про {needed} в чаті."
        )
    if status == "already_matched":
        return f"{verdict.get('message', '')} {verdict.get('hint', '')}".strip()
    if status == "needs_details":
        weak = next(a["verdict"] for a in reversed(attempts) if _needs_details(a.get("verdict") or {}))
        advice = weak.get("ask_owner") or weak["hint"]
        return f"Не можу підтвердити збіг: {weak['message']} {advice}".strip()
    if status == "unresolved":
        # The last attempt may have no verdict: the Matcher found nobody else after a refusal
        refused = next((a["verdict"] for a in reversed(attempts) if a.get("verdict")), {})
        after = "Інших кандидатів немає." if not verdict else f"Спроби вичерпано ({len(attempts)})."
        return f"Надійного збігу не знайдено. Відхилено: {refused.get('message', '')} {after}".strip()
    # no_match
    reason = handoff.get("reason") or (
        "відповідних втрат немає" if handoff.get("direction") == "found" else "відповідних знахідок немає"
    )
    return f"Збігів не знайдено: {reason}"


@traced("lostfound_system")
async def run_system(request: str, model: str | None = None) -> dict:
    """Handle one request end to end.

    status: confirmed | no_match | needs_details | already_matched | unresolved | candidate_only
    """
    record("request", mode="multi", request=request)
    # A free-text request is a new chat post; from here on it goes like "Допоможи знайти L03"
    request, posted = ingest(request)
    if posted:
        log.info("[orchestrator] new post %s: %s", posted["id"], posted["text"])
    attempts: list[dict] = []
    rejected: list[dict] = []
    status, match = "no_match", None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        log.info("[orchestrator] attempt %d/%d", attempt, MAX_ATTEMPTS)
        handoff = await run_matcher(request, rejected=rejected or None, model=model)
        step = {"attempt": attempt, "handoff": handoff, "verdict": None}
        attempts.append(step)

        candidate, own = _sides(handoff)
        if not candidate:
            # After a weak match, "nothing else" means the owner has to add details
            weak = any(_needs_details(r) for r in rejected)
            status = "needs_details" if weak else ("unresolved" if rejected else "no_match")
            break
        if not own:
            # Free-text request: there is no post of the person's own to record a match against
            status = "candidate_only"
            break

        # The Verifier gets the handoff contract plus both posts, read here from the CSV files
        pair = {"lost_id": handoff["lost_id"], "found_id": handoff["found_id"], "reason": handoff["reason"]}
        verdict = await run_verifier(pair, posts=pair_posts(pair["lost_id"], pair["found_id"]), model=model)
        step["verdict"] = verdict
        if verdict["status"] == "confirmed":
            status, match = "confirmed", {"lost_id": handoff["lost_id"], "found_id": handoff["found_id"]}
            break
        if not verdict["retryable"]:
            status = "already_matched"
            break
        rejected.append({
            "candidate_id": candidate,
            "error_code": verdict["error_code"],
            "problem": verdict.get("problem"),
            "message": verdict["message"],
            "hint": verdict["hint"],
        })
    else:
        weak = any(_needs_details(r) for r in rejected)
        status = "needs_details" if weak else "unresolved"

    result = {
        "request": request,
        "posted": posted,
        "status": status,
        "match": match,
        "reply": _reply(status, attempts, match),
        "attempts": attempts,
    }
    record("result", mode="multi", **result)
    log.info("[orchestrator] %s: %s", status, result["reply"])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Matcher + Verifier system on one request")
    parser.add_argument("request", help='e.g. "Допоможи знайти L03"')
    parser.add_argument("--model", default=None, help="Ollama model, default from LOSTFOUND_MODEL")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    log.setLevel(logging.INFO)

    result = asyncio.run(run_system(args.request, model=args.model))
    flush()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
