"""STDIO integration tests for the lost-and-found MCP server."""

import asyncio
import csv
import json
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "src" / "lostfound" / "server_v2.py"
MATCHES = ROOT / "data" / "processed" / "matches.csv"


def _result_value(result):
    # An empty list comes back with no content blocks, only structured content
    if not result.content:
        structured = result.structured_content or {}
        return structured.get("result", structured)
    return json.loads(result.content[0].text)


async def _call_tools(calls: list[tuple[str, dict]]) -> list:
    """Run several tool calls in one server session and return parsed results."""
    server_params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
    results = []
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for name, arguments in calls:
                results.append(_result_value(await session.call_tool(name, arguments)))
    return results


def call(name: str, **arguments):
    return asyncio.run(_call_tools([(name, arguments)]))[0]


def ids(rows: list[dict]) -> list[str]:
    return [row["id"] for row in rows]


@pytest.fixture(autouse=True)
def restore_matches():
    """Every test may write to matches.csv; put the original back afterwards."""
    original = MATCHES.read_bytes()
    yield
    MATCHES.write_bytes(original)


async def _run_checks() -> None:
    server_params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            search_result = await session.call_tool("search_found", {"query": "навушники"})
            found = _result_value(search_result)
            assert isinstance(found, list)
            assert found

            invalid_result = await session.call_tool(
                "confirm_match",
                {"lost_id": "L03", "found_id": "F06", "reason": "перевірка дати"},
            )
            invalid = _result_value(invalid_result)
            assert invalid["status"] == "error"
            assert invalid["error_code"] == "FOUND_BEFORE_LOST"
            assert invalid["hint"]

            success_result = await session.call_tool(
                "confirm_match",
                {"lost_id": "L01", "found_id": "F03", "reason": "аірподси без кейсу, Свєнца 342"},
            )
            success = _result_value(success_result)
            assert success["status"] == "success"

    with MATCHES.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    assert any(row["lost_id"] == "L01" and row["found_id"] == "F03" for row in rows)


def test_mcp_server_stdio() -> None:
    asyncio.run(_run_checks())


# --- search: realistic queries, as the Matcher will phrase them -------------


@pytest.mark.parametrize(
    "query, expected_first",
    [
        # L01 paraphrased: word forms differ from F03 ("аірподси"/"аірподс", "поверх"/"поверсі")
        ("аірподси прошки без кейсу Свєнціцького 3 поверх біля 342", "F03"),
        # L14: "шкіряний" is not in F28, the rest is
        ("чорний шкіряний гаманець СВ 135 аудиторія", "F28"),
        # L04: almost verbatim
        ("коричневі навушники Marshall 222 Свєнціцького", "F09"),
        # L05
        ("біла зарядка type-c тканевий шнур 304 ЦШ", "F10"),
    ],
    ids=["L01-airpods", "L14-wallet", "L04-marshall", "L05-charger"],
)
def test_search_found_long_query_ranks_true_match_first(query, expected_first):
    results = call("search_found", query=query)
    assert results, f"no results for {query!r}"
    assert ids(results)[0] == expected_first, f"got {ids(results)}"


def test_search_lost_long_query_ranks_true_match_first():
    results = call("search_lost", query="ключі від кімнати 519 брелок білий кіт")
    assert results
    assert ids(results)[0] == "L09", f"got {ids(results)}"


@pytest.mark.parametrize(
    "query",
    [
        "зелений гамак складений",  # L17: ground truth says there are no found posts
        "золотий годинник Casio",
    ],
    ids=["L17-hammock", "watch"],
)
def test_search_found_returns_empty_when_nothing_matches(query):
    # Unrelated posts must not be returned as candidates (eval test 3: honest "none")
    assert call("search_found", query=query) == []


def test_search_found_date_from_excludes_earlier_posts():
    # L06 was lost on 2026-10-12; F22 (09.10) is earlier, F23 (12.10) is the true match
    results = call("search_found", query="зелений картхолдер", date_from="2026-10-12")
    assert ids(results) == ["F23"]


def test_search_found_respects_limit():
    assert len(call("search_found", query="навушники", limit=2)) <= 2


# --- confirm_match: each error code -----------------------------------------


def test_confirm_match_missing_reason():
    result = call("confirm_match", lost_id="L01", found_id="F03", reason="")
    assert result["status"] == "error"
    assert result["error_code"] == "MISSING_REASON"


def test_confirm_match_unknown_id():
    result = call("confirm_match", lost_id="L99", found_id="F03", reason="такого оголошення немає")
    assert result["status"] == "error"
    assert result["error_code"] == "UNKNOWN_ID"


def test_confirm_match_already_matched():
    reason = "аірподси без кейсу, Свєнца 342"
    first, second = asyncio.run(_call_tools([
        ("confirm_match", {"lost_id": "L01", "found_id": "F03", "reason": reason}),
        ("confirm_match", {"lost_id": "L01", "found_id": "F03", "reason": reason}),
    ]))
    assert first["status"] == "success"
    assert second["status"] == "error"
    assert second["error_code"] == "ALREADY_MATCHED"


def test_confirm_match_found_before_lost_ground_truth_case():
    # Eval test 4: F22 (09.10) is earlier than L06 (12.10)
    result = call("confirm_match", lost_id="L06", found_id="F22", reason="зелений картхолдер з леокартом у ЦШ")
    assert result["status"] == "error"
    assert result["error_code"] == "FOUND_BEFORE_LOST"
    assert result["hint"]


def test_confirm_match_found_before_lost_then_true_match_succeeds():
    result = call("confirm_match", lost_id="L06", found_id="F23", reason="зелений картхолдер у ЦШ, 12.10")
    assert result["status"] == "success"


@pytest.mark.parametrize(
    "lost_id, found_id",
    [
        ("L10", "F19"),  # ground truth expected_weak_match: "пропуск" with no attributes
        ("L15", "F21"),  # black power bank vs glasses: different items entirely
        ("L13", "F32"),  # grey jacket vs cloakroom token
    ],
)
def test_confirm_match_rejects_weak_or_unrelated_pair(lost_id, found_id):
    result = call("confirm_match", lost_id=lost_id, found_id=found_id, reason="модель вважає, що це збіг")
    assert result["status"] == "error", f"{lost_id}-{found_id} was recorded as a match"
    assert result["error_code"] == "WEAK_MATCH"


# --- error contract (CLAUDE.md): every error carries message, retryable, hint


@pytest.mark.parametrize(
    "arguments",
    [
        {"lost_id": "L01", "found_id": "F03", "reason": ""},  # MISSING_REASON
        {"lost_id": "L99", "found_id": "F03", "reason": "такого оголошення немає"},  # UNKNOWN_ID
        {"lost_id": "L06", "found_id": "F22", "reason": "зелений картхолдер у ЦШ"},  # FOUND_BEFORE_LOST
    ],
)
def test_error_contract_fields(arguments):
    result = call("confirm_match", **arguments)
    assert result["status"] == "error"
    assert result["message"]
    assert isinstance(result["retryable"], bool)
    assert result.get("hint"), f"{result['error_code']} has no hint"
