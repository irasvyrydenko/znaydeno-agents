"""Shared tool-calling loop: MCP tools from the lostfound server, served to a local Ollama model.

Each agent gets only the tools it is allowed to use. A call to any other tool is refused
in code, so the permission split does not depend on the model obeying its prompt.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import ollama
from dotenv import load_dotenv
from fastmcp import Client

from lostfound.server_v2 import mcp as server
from lostfound.tracing import record, traced

load_dotenv()
log = logging.getLogger("lostfound.agents")

DEFAULT_MODEL = "qwen3:8b"


def model_name() -> str:
    return os.getenv("LOSTFOUND_MODEL", DEFAULT_MODEL)


def ollama_client() -> ollama.Client:
    return ollama.Client(host=os.getenv("OLLAMA_HOST", "http://localhost:11434"))


def chat(client: ollama.Client, model: str, messages: list, **kwargs):
    options = {
        "temperature": 0,
        # Ollama's default 4096 is silently exceeded by the prompt plus a few search results,
        # and then the start of the conversation (the instructions) is cut off
        "num_ctx": int(os.getenv("LOSTFOUND_NUM_CTX", "8192")),
        # A runaway generation otherwise never ends
        "num_predict": 1024,
    }
    kwargs = dict(model=model, messages=messages, options=options, **kwargs)
    try:
        # qwen3 "thinking" makes small-model tool calls slower and less stable
        return client.chat(think=False, **kwargs)
    except TypeError:
        return client.chat(**kwargs)


def _simplify_schema(schema: dict) -> dict:
    """Ollama accepts a plain JSON-schema subset: collapse Optional[...] (anyOf with null)."""
    properties = {}
    for name, prop in schema.get("properties", {}).items():
        prop = dict(prop)
        if "anyOf" in prop:
            types = [p["type"] for p in prop.pop("anyOf") if p.get("type") not in (None, "null")]
            if types:
                prop["type"] = types[0]
        prop.pop("default", None)
        properties[name] = prop
    return {"type": "object", "properties": properties, "required": schema.get("required", [])}


def to_ollama_tool(tool) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": _simplify_schema(tool.input_schema),
        },
    }


def _summarize(result: Any) -> str:
    if isinstance(result, list):
        found = [row.get("id", "?") for row in result if isinstance(row, dict)]
        return f"{len(result)} result(s): {', '.join(found)}" if result else "0 results"
    if isinstance(result, dict) and result.get("status") == "error":
        return f"error {result.get('error_code')}: {result.get('message')}"
    return json.dumps(result, ensure_ascii=False, default=str)[:200]


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict
    result: Any


@dataclass
class AgentRun:
    messages: list
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    final_text: str = ""
    steps_exhausted: bool = False

    def returned_ids(self, tool_name: str) -> set[str]:
        """Ids of posts that a tool actually returned during this run."""
        ids = set()
        for call in self.tool_calls:
            if call.name == tool_name and isinstance(call.result, list):
                ids.update(row["id"] for row in call.result if isinstance(row, dict) and "id" in row)
        return ids


@traced("agent_loop")
async def run_tool_loop(
    agent: str,
    system: str,
    user: str,
    allowed_tools: set[str],
    model: str | None = None,
    max_steps: int = 6,
    sanitize_arguments: Callable[[str, dict], dict] | None = None,
) -> AgentRun:
    """Let the model call its allowed tools until it answers in text or max_steps runs out.

    sanitize_arguments(tool_name, arguments) may fix arguments the model made up
    before the tool is called; the change is logged.
    """
    model = model or model_name()
    client = ollama_client()
    messages: list = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    run = AgentRun(messages)

    async with Client(server) as mcp_client:
        tools = [t for t in await mcp_client.list_tools() if t.name in allowed_tools]
        missing = allowed_tools - {t.name for t in tools}
        if missing:
            raise RuntimeError(f"MCP server does not expose: {sorted(missing)}")
        ollama_tools = [to_ollama_tool(t) for t in tools]
        seen_calls: set[str] = set()
        repeats = 0

        for _ in range(max_steps):
            response = chat(client, model, messages, tools=ollama_tools)
            messages.append(response.message)
            calls = response.message.tool_calls or []
            if not calls:
                run.final_text = (response.message.content or "").strip()
                return run

            for call in calls:
                name = call.function.name
                arguments = dict(call.function.arguments or {})
                if sanitize_arguments:
                    fixed = sanitize_arguments(name, arguments)
                    if fixed != arguments:
                        log.info("[%s] model asked %s(%s), corrected", agent, name,
                                 json.dumps(arguments, ensure_ascii=False))
                        arguments = fixed
                log.info("[%s] -> %s(%s)", agent, name, json.dumps(arguments, ensure_ascii=False))

                started = time.perf_counter()
                call_key = f"{name}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"
                if call_key in seen_calls:
                    # Small models loop on the same query; answer from history instead of calling again
                    repeats += 1
                    result = {
                        "status": "error",
                        "error_code": "REPEATED_CALL",
                        "message": "Цей виклик уже виконано, результат є вище. "
                        "Зміни запит або дай фінальну відповідь.",
                    }
                elif name not in allowed_tools:
                    result: Any = {
                        "status": "error",
                        "error_code": "TOOL_NOT_ALLOWED",
                        "message": f"Інструмент {name} недоступний агенту {agent}.",
                    }
                else:
                    try:
                        response_tool = await mcp_client.call_tool(name, arguments, raise_on_error=False)
                        if response_tool.is_error:
                            text = " ".join(getattr(c, "text", "") for c in response_tool.content)
                            result = {"status": "error", "error_code": "TOOL_FAILED", "message": text}
                        else:
                            result = response_tool.data
                    except Exception as e:  # bad arguments, server crash
                        result = {"status": "error", "error_code": "TOOL_FAILED", "message": str(e)}

                seen_calls.add(call_key)
                log.info("[%s] <- %s", agent, _summarize(result))
                record("tool_call", agent=agent, tool=name, arguments=arguments, result=result,
                       ms=round((time.perf_counter() - started) * 1000))
                run.tool_calls.append(ToolCallRecord(name, arguments, result))
                messages.append({
                    "role": "tool",
                    "tool_name": name,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })

            if repeats >= 2:
                log.info("[%s] stopped: the model keeps repeating the same call", agent)
                return run

    run.steps_exhausted = True
    log.info("[%s] stopped after %d steps without a final answer", agent, max_steps)
    return run


def ask_json(messages: list, instruction: str, schema: dict, model: str | None = None) -> dict:
    """Ask for a final answer constrained to a JSON schema (no tools offered)."""
    response = chat(
        ollama_client(),
        model or model_name(),
        [*messages, {"role": "user", "content": instruction}],
        format=schema,
    )
    try:
        return json.loads(response.message.content or "{}")
    except json.JSONDecodeError:
        log.warning("model returned invalid JSON: %r", response.message.content)
        return {}
