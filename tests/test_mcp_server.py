"""STDIO integration tests for the lost-and-found MCP server."""

import asyncio
import csv
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "src" / "lostfound" / "server.py"
MATCHES = ROOT / "data" / "processed" / "matches.csv"


def _result_value(result):
    return json.loads(result.content[0].text)


async def _run_checks() -> None:
    original_matches = MATCHES.read_bytes()
    server_params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
    try:
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
    finally:
        MATCHES.write_bytes(original_matches)


def test_mcp_server_stdio() -> None:
    asyncio.run(_run_checks())