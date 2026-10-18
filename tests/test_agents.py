"""Agent plumbing tests with a scripted fake LLM: no Ollama needed.

The MCP server is real (in-memory FastMCP client), so these check that the Verifier goes
through MCP, that it cannot change the pair, that the verdict comes from tool results,
and that the orchestrator loop follows hints and stops correctly.
"""

import asyncio
import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lostfound.agents import base, orchestrator, verifier

ROOT = Path(__file__).resolve().parents[1]
MATCHES = ROOT / "data" / "processed" / "matches.csv"


@pytest.fixture(autouse=True)
def restore_matches():
    original = MATCHES.read_bytes()
    with MATCHES.open("w", encoding="utf-8", newline="") as file:
        csv.writer(file).writerow(["lost_id", "found_id", "reason", "confirmed_at"])
    yield
    MATCHES.write_bytes(original)


def rows():
    with MATCHES.open(encoding="utf-8", newline="") as file:
        return [(r["lost_id"], r["found_id"]) for r in csv.DictReader(file)]


def tool_call(name, **arguments):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))


def reply(content="", calls=None):
    return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls or []))


def script_llm(monkeypatch, responses):
    """Replace the model with a list of canned responses, returned in order."""
    queue = list(responses)
    monkeypatch.setattr(base, "chat", lambda *a, **k: queue.pop(0))
    return queue


def test_verifier_confirms_through_mcp(monkeypatch):
    script_llm(monkeypatch, [
        reply(calls=[tool_call("confirm_match", lost_id="L03", found_id="F07",
                               reason="Чорні навушники JBL, церква, та сама дата.")]),
        reply("Записано."),
    ])
    handoff = {"lost_id": "L03", "found_id": "F07", "reason": "JBL у церкві"}
    verdict = asyncio.run(verifier.run_verifier(handoff))
    assert verdict["status"] == "confirmed"
    assert rows() == [("L03", "F07")]


def test_verifier_cannot_swap_ids(monkeypatch):
    script_llm(monkeypatch, [
        reply(calls=[tool_call("confirm_match", lost_id="L03", found_id="F01",
                               reason="Насправді краще інша пара навушників.")]),
        reply("Готово."),
    ])
    handoff = {"lost_id": "L03", "found_id": "F07", "reason": "JBL у церкві"}
    verdict = asyncio.run(verifier.run_verifier(handoff))
    assert verdict["status"] == "confirmed"
    assert rows() == [("L03", "F07")]


def test_verifier_has_no_search_tools(monkeypatch):
    script_llm(monkeypatch, [
        reply(calls=[tool_call("search_found", query="навушники")]),
        reply(""),
        reply(json.dumps({"decision": "reject", "message": "Немає деталей."})),
    ])
    handoff = {"lost_id": "L03", "found_id": "F07", "reason": "схоже"}
    verdict = asyncio.run(verifier.run_verifier(handoff))
    assert verdict["status"] == "declined"
    assert rows() == []


def test_verifier_reports_tool_refusal_with_hint(monkeypatch):
    script_llm(monkeypatch, [
        reply(calls=[tool_call("confirm_match", lost_id="L06", found_id="F22",
                               reason="Зелений картхолдер з леокартом у ЦШ.")]),
        reply("Відмова."),
    ])
    handoff = {"lost_id": "L06", "found_id": "F22", "reason": "картхолдер ЦШ"}
    verdict = asyncio.run(verifier.run_verifier(handoff))
    assert verdict["status"] == "rejected"
    assert verdict["error_code"] == "FOUND_BEFORE_LOST"
    assert "F23" in verdict["hint"]
    assert rows() == []


def test_orchestrator_follows_hint_to_next_candidate(monkeypatch):
    proposals = iter(["F22", "F23"])
    seen_rejected = []

    async def fake_matcher(request, rejected=None, model=None):
        seen_rejected.append(list(rejected or []))
        return {"lost_id": "L06", "found_id": next(proposals), "reason": "зелений картхолдер ЦШ"}

    async def fake_verifier(handoff, model=None):
        if handoff["found_id"] == "F22":
            return {"status": "rejected", "error_code": "FOUND_BEFORE_LOST", "message": "дата",
                    "hint": "Інші кандидати: F23", "retryable": True, **handoff}
        return {"status": "confirmed", "error_code": None, "message": "ок", "hint": "",
                "retryable": True, **handoff}

    monkeypatch.setattr(orchestrator, "run_matcher", fake_matcher)
    monkeypatch.setattr(orchestrator, "run_verifier", fake_verifier)
    result = asyncio.run(orchestrator.run_system("Допоможи знайти L06"))
    assert result["status"] == "confirmed"
    assert result["match"] == {"lost_id": "L06", "found_id": "F23"}
    assert seen_rejected[1][0]["hint"] == "Інші кандидати: F23"


def test_orchestrator_no_candidate_skips_verifier(monkeypatch):
    async def fake_matcher(request, rejected=None, model=None):
        return {"lost_id": "L17", "found_id": None, "reason": "немає знахідок"}

    async def fail_verifier(handoff, model=None):
        raise AssertionError("Verifier must not be called without a candidate")

    monkeypatch.setattr(orchestrator, "run_matcher", fake_matcher)
    monkeypatch.setattr(orchestrator, "run_verifier", fail_verifier)
    result = asyncio.run(orchestrator.run_system("Допоможи знайти L17"))
    assert result["status"] == "no_match"
    assert "немає знахідок" in result["reply"]


def test_orchestrator_weak_match_asks_for_details_and_stops_at_cap(monkeypatch):
    calls = {"verifier": 0}

    async def fake_matcher(request, rejected=None, model=None):
        return {"lost_id": "L10", "found_id": f"F{19 + len(rejected or [])}", "reason": "пропуск"}

    async def fake_verifier(handoff, model=None):
        calls["verifier"] += 1
        return {"status": "rejected", "error_code": "WEAK_MATCH", "message": "мало деталей",
                "hint": "Запитайте у власника колір.", "retryable": True, **handoff}

    monkeypatch.setattr(orchestrator, "run_matcher", fake_matcher)
    monkeypatch.setattr(orchestrator, "run_verifier", fake_verifier)
    result = asyncio.run(orchestrator.run_system("Допоможи знайти L10"))
    assert calls["verifier"] == orchestrator.MAX_ATTEMPTS
    assert result["status"] == "needs_details"
    assert "Запитайте у власника" in result["reply"]


# --- found direction: "I found something, whose is it?" --------------------------------

from lostfound.agents import matcher  # noqa: E402


@pytest.mark.parametrize("request_text, direction", [
    ("Допоможи знайти L03", "lost"),
    ("Чиє це? F07", "found"),
    ("загубила чорні навушники в К2, якщо знайдете — напишіть", "lost"),
    ("Хтось знайшов мої навушники?", "lost"),
    ("Знайшла читацький на підвіконні в 242", "found"),
    ("Хто загубив ключі з котиком? Лежать на охороні К1", "found"),
    ("чия парасолька у світлиці К2?", "found"),
])
def test_detect_direction(request_text, direction):
    assert matcher.detect_direction(request_text) == direction


def test_date_guard_found_keeps_only_date_to():
    sanitize = matcher.date_guard({"event_date": "2026-10-08"}, "found")
    fixed = sanitize("search_lost", {"query": "JBL", "date_from": "2026-01-01", "date_to": "2030-01-01"})
    assert fixed == {"query": "JBL", "date_to": "2026-10-08"}


def test_matcher_found_direction_searches_lost(monkeypatch):
    script_llm(monkeypatch, [
        reply(calls=[tool_call("search_lost", query="навушники JBL церква")]),
        reply("L03 підходить."),
        reply(json.dumps({"lost_id": "L03", "found_id": "", "reason": "Чорні JBL у церкві, та сама дата."})),
    ])
    handoff = asyncio.run(matcher.run_matcher("Чиє це? F07"))
    assert handoff == {"lost_id": "L03", "found_id": "F07",
                       "reason": "Чорні JBL у церкві, та сама дата.", "direction": "found"}


def test_matcher_found_direction_drops_invented_lost_id(monkeypatch):
    script_llm(monkeypatch, [
        reply(calls=[tool_call("search_lost", query="навушники JBL церква")]),
        reply("L09 підходить."),
        reply(json.dumps({"lost_id": "L09", "found_id": "F07", "reason": "вигадка"})),
    ])
    handoff = asyncio.run(matcher.run_matcher("Чиє це? F07"))
    assert handoff["lost_id"] is None and handoff["found_id"] == "F07"


def test_orchestrator_found_direction_retries_on_lost_side(monkeypatch):
    proposals = iter(["L05", "L06"])
    seen_rejected, verifier_inputs = [], []

    async def fake_matcher(request, rejected=None, model=None):
        seen_rejected.append(list(rejected or []))
        return {"lost_id": next(proposals), "found_id": "F23", "reason": "картхолдер ЦШ", "direction": "found"}

    async def fake_verifier(handoff, model=None):
        verifier_inputs.append(handoff)
        if handoff["lost_id"] == "L05":
            return {"status": "rejected", "error_code": "WEAK_MATCH", "message": "інша річ",
                    "hint": "Інші втрати, схожі на F23: L06", "retryable": True, **handoff}
        return {"status": "confirmed", "error_code": None, "message": "ок", "hint": "",
                "retryable": True, **handoff}

    monkeypatch.setattr(orchestrator, "run_matcher", fake_matcher)
    monkeypatch.setattr(orchestrator, "run_verifier", fake_verifier)
    result = asyncio.run(orchestrator.run_system("Чиє це? F23"))
    assert result["match"] == {"lost_id": "L06", "found_id": "F23"}
    assert seen_rejected[1][0]["candidate_id"] == "L05"
    # The Verifier sees only the handoff contract, not the direction
    assert set(verifier_inputs[0]) == {"lost_id", "found_id", "reason"}


def test_orchestrator_found_free_text_does_not_write(monkeypatch):
    async def fake_matcher(request, rejected=None, model=None):
        return {"lost_id": "L07", "found_id": None, "reason": "читацький у 242", "direction": "found"}

    async def fail_verifier(handoff, model=None):
        raise AssertionError("nothing to record without a found post")

    monkeypatch.setattr(orchestrator, "run_matcher", fake_matcher)
    monkeypatch.setattr(orchestrator, "run_verifier", fail_verifier)
    result = asyncio.run(orchestrator.run_system("Знайшла читацький у 242, чий?"))
    assert result["status"] == "candidate_only"
    assert "L07" in result["reply"] and "F.." in result["reply"]


def test_hint_offers_lost_side_alternatives():
    from lostfound.server_v2 import confirm_match
    tool = getattr(confirm_match, "fn", confirm_match)
    result = tool("L06", "F22", "Зелений картхолдер з леокартом у ЦШ.")
    assert result["error_code"] == "FOUND_BEFORE_LOST"
    assert "F23" in result["hint"]


def test_search_ignores_posts_sharing_only_the_place():
    from lostfound.server_v2 import search_lost, search_found
    lost = getattr(search_lost, "fn", search_lost)
    found = getattr(search_found, "fn", search_found)
    assert lost("цноту 242 Свєнціцького") == []
    assert [r["id"] for r in lost("читацький 242 Свєнціцького")][:1] == ["L07"]
    # A query that names only a place still lists what was found there
    assert found("К2")


def test_misspelled_arguments_are_dropped_not_failed(monkeypatch):
    script_llm(monkeypatch, [
        reply(calls=[tool_call("search_found", query="навушники JBL церква", **{"date,To": "2026-10-18"})]),
        reply("F07."),
        reply(json.dumps({"lost_id": "L03", "found_id": "F07", "reason": "JBL у церкві"})),
    ])
    handoff = asyncio.run(matcher.run_matcher("Допоможи знайти L03"))
    assert handoff["found_id"] == "F07"
