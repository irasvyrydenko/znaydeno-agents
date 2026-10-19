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
def isolate_user_posts(monkeypatch, tmp_path):
    """Posts created from free-text requests go to a temporary folder, not data/processed/."""
    from lostfound import server_v2
    for name in list(server_v2.USER_POSTS):
        monkeypatch.setitem(server_v2.USER_POSTS, name, tmp_path / f"user_{name}")


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


def check(**overrides):
    """A Verifier check that passes, as the model would return it."""
    base_check = {"lost_item": "навушники", "found_item": "навушники", "same_item": True,
                  "lost_place": "", "found_place": "", "place_conflict": False, "contradictions": [],
                  "shared_specific": ["чорні"], "missing_details": ""}
    return reply(json.dumps({**base_check, **overrides}))


def test_verifier_confirms_through_mcp(monkeypatch):
    script_llm(monkeypatch, [
        check(),
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
        check(),
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
        check(),
        reply(calls=[tool_call("search_found", query="навушники")]),
        reply(""),
    ])
    handoff = {"lost_id": "L03", "found_id": "F07", "reason": "схоже"}
    verdict = asyncio.run(verifier.run_verifier(handoff))
    assert verdict["status"] == "declined"
    assert rows() == []


def test_verifier_reports_tool_refusal_with_hint(monkeypatch):
    script_llm(monkeypatch, [
        check(),
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

    async def fake_verifier(handoff, posts=None, model=None):
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

    async def fail_verifier(handoff, posts=None, model=None):
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

    async def fake_verifier(handoff, posts=None, model=None):
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

    async def fake_verifier(handoff, posts=None, model=None):
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


def test_free_text_becomes_a_post_and_reaches_the_verifier(monkeypatch):
    seen = {}

    async def fake_matcher(request, rejected=None, model=None):
        seen["request"] = request
        new_id = request.split(":")[0]
        return {"lost_id": "L07", "found_id": new_id, "reason": "читацький у 242", "direction": "found"}

    async def fake_verifier(handoff, posts=None, model=None):
        seen["posts"] = posts
        return {"status": "confirmed", "error_code": None, "message": "ок", "hint": "", "retryable": True, **handoff}

    monkeypatch.setattr(orchestrator, "run_matcher", fake_matcher)
    monkeypatch.setattr(orchestrator, "run_verifier", fake_verifier)
    result = asyncio.run(orchestrator.run_system("Знайшла читацький у 242, чий?"))
    new_id = result["posted"]["id"]
    assert new_id == "F33"  # the dataset ends at F32
    assert seen["request"].startswith("F33: ")
    assert seen["posts"]["found"]["text"] == "Знайшла читацький у 242, чий?"
    assert result["match"] == {"lost_id": "L07", "found_id": "F33"}


def test_ingest_leaves_requests_with_ids_alone():
    from lostfound.agents.posts import ingest
    assert ingest("Допоможи знайти L03") == ("Допоможи знайти L03", None)
    request, post = ingest("загубила зарядку в трапезній")
    assert post["id"] == "L26" and request == "L26: загубила зарядку в трапезній"
    request, post = ingest("загубила ще й парасольку")
    assert post["id"] == "L27"


def test_new_post_is_searchable_and_matchable():
    from lostfound.agents.posts import add_post
    from lostfound.server_v2 import confirm_match, search_lost
    post = add_post("lost", "загубила зарядку лайтнінг у трапезній на Свєнца")
    assert post["id"] in [r["id"] for r in getattr(search_lost, "fn", search_lost)("зарядка лайтнінг трапезній")]
    result = getattr(confirm_match, "fn", confirm_match)(post["id"], "F04", "Зарядка лайтнінг у трапезній на Свєнца.")
    assert result["status"] == "success"
    assert rows() == [(post["id"], "F04")]


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


def test_verifier_sees_both_posts(monkeypatch):
    seen = []

    def fake_chat(client, model, messages, **kwargs):
        seen.append(messages[1]["content"])
        return check(same_item=False) if "format" in kwargs else reply("")

    monkeypatch.setattr(base, "chat", fake_chat)
    handoff = {"lost_id": "L03", "found_id": "F07", "reason": "Червоні навушники"}
    verdict = asyncio.run(verifier.run_verifier(handoff))
    assert "JBL" in seen[0] and "в церкві на кріслі" in seen[0]
    assert verdict["status"] == "declined" and verdict["problem"] == "different_item"
    assert rows() == []


def test_repeated_call_does_not_hide_the_real_refusal(monkeypatch):
    early = tool_call("confirm_match", lost_id="L06", found_id="F22", reason="Зелений картхолдер з леокартом у ЦШ.")
    script_llm(monkeypatch, [check(), reply(calls=[early]), reply(calls=[early]), reply("Відмова.")])
    handoff = {"lost_id": "L06", "found_id": "F22", "reason": "картхолдер ЦШ"}
    verdict = asyncio.run(verifier.run_verifier(handoff))
    assert verdict["status"] == "rejected"
    assert verdict["error_code"] == "FOUND_BEFORE_LOST"
    assert "F23" in verdict["hint"]


def test_orchestrator_passes_posts_and_treats_vague_as_needs_details(monkeypatch):
    seen_posts = []

    async def fake_matcher(request, rejected=None, model=None):
        if rejected:
            return {"lost_id": "L10", "found_id": None, "reason": "інших немає", "direction": "lost"}
        return {"lost_id": "L10", "found_id": "F19", "reason": "пропуск", "direction": "lost"}

    async def fake_verifier(handoff, posts=None, model=None):
        seen_posts.append(posts)
        return {"status": "declined", "error_code": "VERIFIER_DECLINED", "problem": "too_vague",
                "message": "Лише «пропуск».", "hint": "Запитайте у власника: колір шнурка.",
                "retryable": True, **handoff}

    monkeypatch.setattr(orchestrator, "run_matcher", fake_matcher)
    monkeypatch.setattr(orchestrator, "run_verifier", fake_verifier)
    result = asyncio.run(orchestrator.run_system("Допоможи знайти L10"))
    assert seen_posts[0]["lost"]["id"] == "L10" and seen_posts[0]["found"]["id"] == "F19"
    assert result["status"] == "needs_details"
    assert "колір шнурка" in result["reply"]


@pytest.mark.parametrize("overrides, problem", [
    ({"same_item": False}, "different_item"),
    ({"place_conflict": True, "lost_place": "242", "found_place": "Стрийський парк"}, "contradiction"),
    ({"contradictions": ["lost: чорні / found: білі"]}, "contradiction"),
    ({}, None),
])
def test_judge(overrides, problem):
    passing = json.loads(check().message.content)
    posts = {"lost": {"id": "L03", "text": "загубила чорні великі JBL навушники в церкві"},
             "found": {"id": "F07", "text": "в церкві на кріслі лежать великі чорні навушники JBL"}}
    got, message = verifier.judge({**passing, **overrides}, {"lost_id": "L03", "found_id": "F07"}, posts)
    assert got == problem
    assert bool(message) == bool(problem)


@pytest.mark.parametrize("lost_place, found_place, same", [
    ("Свєнціцького, 3 поверх біля 342, або трапезну", "3 поверсі Свєнціцького біля 342", True),
    ("211 на СВ", "трапезній на Свєнца", False),
    ("в 242 на Свєнціцького", "в Стрийському парку", False),
    ("1 поверх СВ", "3 поверх СВ", False),
])
def test_same_spot(lost_place, found_place, same):
    assert verifier._same_spot(lost_place, found_place) is same


def test_orchestrator_unresolved_reply_names_the_refusal(monkeypatch):
    calls = iter([
        {"lost_id": "L15", "found_id": "F30", "reason": "павербанк 140", "direction": "lost"},
        {"lost_id": "L15", "found_id": None, "reason": "інших немає", "direction": "lost"},
    ])

    async def fake_matcher(request, rejected=None, model=None):
        return next(calls)

    async def fake_verifier(handoff, posts=None, model=None):
        return {"status": "declined", "error_code": "VERIFIER_DECLINED", "problem": "different_item",
                "message": "Це різні речі: чорний і білий павербанк.", "hint": "", "retryable": True, **handoff}

    monkeypatch.setattr(orchestrator, "run_matcher", fake_matcher)
    monkeypatch.setattr(orchestrator, "run_verifier", fake_verifier)
    result = asyncio.run(orchestrator.run_system("Допоможи знайти L15"))
    assert result["status"] == "unresolved"
    assert "чорний і білий павербанк" in result["reply"]
    assert "Інших кандидатів немає" in result["reply"]


def test_no_place_conflict_when_one_post_names_no_place(monkeypatch):
    # L10 names no place, so the refusal must be "too vague", not "different places"
    script_llm(monkeypatch, [check(place_conflict=True, lost_place="", found_place="на столі в трапезній",
                                   shared_specific=[], missing_details="колір і бренд")])
    verdict = asyncio.run(verifier.run_verifier({"lost_id": "L10", "found_id": "F19", "reason": "пропуск"}))
    assert verdict["status"] == "declined" and verdict["problem"] == "too_vague"
    assert "колір і бренд" in verdict["hint"] and "колір і бренд" in verdict["ask_owner"]


@pytest.mark.parametrize("text, kept", [
    ("lost: чорні / found: білі", True),
    ("lost: / / found: коричневі", False),
    ("lost:  / found: Marshall", False),
    ("колір різний", True),
])
def test_contradiction_needs_both_sides(text, kept):
    assert verifier._both_sides(text) is kept


def test_place_quoted_from_the_other_post_is_ignored():
    post = {"text": "загубила чорні навушники"}
    assert verifier._quoted_from("віднесла в охоронця К2", post) == ""
    assert verifier._quoted_from("в 242 на Свєнціцького", {"text": "Забула його в 242 на Свєнціцького"}) == "в 242 на Свєнціцького"


@pytest.mark.parametrize("lost_text, found_text, problem", [
    ("загубила чорні навушники", "в церкві на кріслі лежать великі чорні навушники JBL", "too_vague"),
    ("загубила ключ-пропуск", "пропуск лежав на столі в трапезній", "too_vague"),
    ("загубила зелений картхолдер з леокартом у ЦШ", "зелений картхолдер на сходах у ЦШ", None),
    ("ніхто не бачив сіру куртку? залишила в лекторії", "знайдена сіра куртка в лекторії", None),
])
def test_judge_needs_more_than_colour(lost_text, found_text, problem):
    passing = json.loads(check().message.content)
    posts = {"lost": {"id": "L99", "text": lost_text}, "found": {"id": "F99", "text": found_text}}
    assert verifier.judge(passing, {"lost_id": "L99", "found_id": "F99"}, posts)[0] == problem
