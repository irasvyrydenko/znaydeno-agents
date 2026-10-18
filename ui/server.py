"""Live visualization of the Matcher + Verifier system: a chat on the left, the architecture
diagram on the right, lighting up as the agents think and call MCP tools.

The agents' code is not changed. This process wraps a few functions from the outside
(the LLM call, the MCP client's call_tool, the trace recorder, run_matcher/run_verifier)
and streams every step to the browser as NDJSON.

Usage:
    .venv/bin/python ui/server.py            # then open http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from lostfound.agents import base, orchestrator
from lostfound.server_v2 import DATA_DIR, MATCH_FIELDS, MATCHES_PATH

HERE = Path(__file__).resolve().parent

# --- event sink -----------------------------------------------------------------------
# One request runs at a time: the hooks below are process-wide.

_run_lock = threading.Lock()
_sink: queue.Queue | None = None
_current_agent = {"name": None}
_started = 0.0


def emit(kind: str, **data: Any) -> None:
    if _sink is None:
        return
    event = {"type": kind, "t": round(time.perf_counter() - _started, 3), **data}
    _sink.put(json.loads(json.dumps(event, ensure_ascii=False, default=str)))


# --- hooks around the unchanged agent code ---------------------------------------------

_original_chat = base.chat


def _chat(*args, **kwargs):
    agent = _current_agent["name"]
    final = "format" in kwargs  # ask_json: the structured final answer, no tools
    emit("llm_start", agent=agent, final=final)
    try:
        return _original_chat(*args, **kwargs)
    finally:
        emit("llm_end", agent=agent)


base.chat = _chat

_original_call_tool = base.Client.call_tool


async def _call_tool(self, name, arguments=None, *args, **kwargs):
    emit("tool_start", agent=_current_agent["name"], tool=name, arguments=arguments or {})
    return await _original_call_tool(self, name, arguments, *args, **kwargs)


base.Client.call_tool = _call_tool

_original_record = base.record


def _record(event: str, **data: Any) -> None:
    _original_record(event, **data)
    if event == "tool_call":
        emit("tool_end", **data)


base.record = _record

_original_matcher = orchestrator.run_matcher
_original_verifier = orchestrator.run_verifier


async def _run_matcher(request, rejected=None, model=None):
    _current_agent["name"] = "matcher"
    emit("agent_start", agent="matcher", rejected=rejected or [])
    handoff = await _original_matcher(request, rejected=rejected, model=model)
    emit("handoff", handoff=handoff)
    _current_agent["name"] = None
    return handoff


async def _run_verifier(handoff, model=None):
    _current_agent["name"] = "verifier"
    emit("agent_start", agent="verifier", handoff=handoff)
    verdict = await _original_verifier(handoff, model=model)
    emit("verdict", verdict=verdict)
    _current_agent["name"] = None
    return verdict


orchestrator.run_matcher = _run_matcher
orchestrator.run_verifier = _run_verifier


class _ConsoleHandler(logging.Handler):
    """The same console lines as in the terminal, for the log panel."""

    def emit(self, record: logging.LogRecord) -> None:
        emit("log", message=record.getMessage())


base.log.setLevel(logging.INFO)
base.log.addHandler(_ConsoleHandler())


# --- data --------------------------------------------------------------------------------


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def _data() -> dict:
    return {
        "lost": _rows(DATA_DIR / "lost.csv"),
        "found": _rows(DATA_DIR / "found.csv"),
        "matches": _rows(MATCHES_PATH),
    }


# --- routes --------------------------------------------------------------------------------


async def index(request: Request):
    return FileResponse(HERE / "index.html")


async def data(request: Request):
    return JSONResponse(_data())


async def reset(request: Request):
    if _run_lock.locked():
        return JSONResponse({"error": "Зачекайте, поки завершиться запит."}, status_code=409)
    with MATCHES_PATH.open("w", encoding="utf-8", newline="") as file:
        csv.DictWriter(file, fieldnames=MATCH_FIELDS).writeheader()
    return JSONResponse(_data())


def _worker(text: str, sink: queue.Queue) -> None:
    global _sink, _started
    _sink, _started = sink, time.perf_counter()
    try:
        emit("request", text=text)
        result = asyncio.run(orchestrator.run_system(text))
        emit("result", result=result, matches=_rows(MATCHES_PATH))
    except Exception as e:  # show it in the chat instead of a silent hang
        logging.exception("run failed")
        emit("error", message=f"{type(e).__name__}: {e}")
    finally:
        _current_agent["name"] = None
        sink.put(None)
        _sink = None
        _run_lock.release()


async def chat(request: Request):
    body = await request.json()
    text = str(body.get("text", "")).strip()
    if not text:
        return JSONResponse({"error": "Порожній запит."}, status_code=400)
    if not _run_lock.acquire(blocking=False):
        return JSONResponse({"error": "Агенти ще обробляють попередній запит."}, status_code=409)

    sink: queue.Queue = queue.Queue()
    # The Ollama client is blocking, so the agents run in their own thread and event loop
    threading.Thread(target=_worker, args=(text, sink), daemon=True).start()

    async def stream():
        while True:
            event = await asyncio.to_thread(sink.get)
            if event is None:
                return
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


app = Starlette(routes=[
    Route("/", index),
    Route("/api/data", data),
    Route("/api/reset", reset, methods=["POST"]),
    Route("/api/chat", chat, methods=["POST"]),
])


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    print("Знайдено! UI: http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
