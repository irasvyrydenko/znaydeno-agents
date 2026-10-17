"""Smoke test: does the local Ollama model call tools reliably?

Checks, with a fake `search_found` tool (no project data is read):
  1. A lost-item request makes the model call search_found with non-empty text.
  2. The tool result goes back and the model produces a final text answer.
  3. A greeting does NOT trigger a tool call.
  4. Check 1 is repeated N times to see how stable the calling is.

Usage:
    python scripts/smoke_ollama.py [--model qwen3:8b] [--runs 5]

Model and host come from .env (LOSTFOUND_MODEL, OLLAMA_HOST) unless overridden.
Exit code is 0 only if every check passes.
"""
import argparse
import json
import os
import sys
import time

import ollama
from dotenv import load_dotenv

SEARCH_FOUND_TOOL = {
    "type": "function",
    "function": {
        "name": "search_found",
        "description": "Search found-item posts by free text. Use it when the user asks to find a lost item.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Description of the lost item and place"},
                "since": {"type": "string", "description": "Optional ISO date, only posts from this date on"},
            },
            "required": ["text"],
        },
    },
}

SYSTEM = (
    "You help students find lost items. You have a search_found tool. "
    "When the user describes a lost item, call search_found. Otherwise answer briefly."
)
LOST_REQUEST = "Загубила аірподси без кейсу, десь на 3 поверсі Свєнціцького біля 342. Допоможи знайти."
GREETING = "Привіт! Що ти вмієш?"
FAKE_RESULT = [{"id": "F03", "event_date": "2026-10-06",
                "text": "знайшла аірподс без кейсу на 3 поверсі Свєнціцького біля 342"}]


def chat(client: ollama.Client, model: str, messages: list, tools: list):
    kwargs = dict(model=model, messages=messages, tools=tools, options={"temperature": 0})
    try:
        # qwen3 "thinking" makes small-model tool calls slower and less stable
        return client.chat(think=False, **kwargs)
    except TypeError:
        return client.chat(**kwargs)


def tool_calls(response) -> list:
    return response.message.tool_calls or []


def check_call(client, model) -> tuple[bool, str, list]:
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": LOST_REQUEST}]
    response = chat(client, model, messages, [SEARCH_FOUND_TOOL])
    calls = tool_calls(response)
    if not calls:
        return False, f"no tool call, model said: {(response.message.content or '')[:120]!r}", messages
    call = calls[0]
    args = dict(call.function.arguments)
    if call.function.name != "search_found":
        return False, f"wrong tool: {call.function.name}", messages
    if not str(args.get("text", "")).strip():
        return False, f"empty text argument: {args}", messages
    messages.append(response.message)
    return True, f"search_found({json.dumps(args, ensure_ascii=False)})", messages


def check_final_answer(client, model, messages) -> tuple[bool, str]:
    messages.append({"role": "tool", "tool_name": "search_found",
                     "content": json.dumps(FAKE_RESULT, ensure_ascii=False)})
    response = chat(client, model, messages, [SEARCH_FOUND_TOOL])
    text = (response.message.content or "").strip()
    if not text:
        return False, "empty final answer"
    return True, text[:160].replace("\n", " ")


def check_no_call(client, model) -> tuple[bool, str]:
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": GREETING}]
    response = chat(client, model, messages, [SEARCH_FOUND_TOOL])
    if tool_calls(response):
        return False, f"unexpected tool call: {tool_calls(response)[0].function.name}"
    return True, (response.message.content or "").strip()[:160].replace("\n", " ")


def report(name: str, ok: bool, detail: str, seconds: float) -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name} ({seconds:.1f}s): {detail}")


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--model", default=os.getenv("LOSTFOUND_MODEL", "qwen3:8b"))
    parser.add_argument("--host", default=os.getenv("OLLAMA_HOST", "http://localhost:11434"))
    parser.add_argument("--runs", type=int, default=5, help="repetitions for the stability check")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    client = ollama.Client(host=args.host)
    print(f"model={args.model} host={args.host}\n")

    try:
        client.show(args.model)
    except ollama.ResponseError:
        print(f"Model {args.model!r} not found. Run: ollama pull {args.model}")
        return 2
    except Exception as e:  # connection refused, etc.
        print(f"Cannot reach Ollama at {args.host}: {e}")
        return 2

    failures = 0

    t = time.time()
    ok, detail, messages = check_call(client, args.model)
    report("tool call on lost-item request", ok, detail, time.time() - t)
    failures += not ok

    if ok:
        t = time.time()
        ok2, detail2 = check_final_answer(client, args.model, messages)
        report("final answer after tool result", ok2, detail2, time.time() - t)
        failures += not ok2

    t = time.time()
    ok3, detail3 = check_no_call(client, args.model)
    report("no tool call on greeting", ok3, detail3, time.time() - t)
    failures += not ok3

    passed = 0
    t = time.time()
    for _ in range(args.runs):
        passed += check_call(client, args.model)[0]
    ok4 = passed == args.runs
    report(f"stability: {args.runs} runs", ok4, f"{passed}/{args.runs} produced a valid call", time.time() - t)
    failures += not ok4

    print("\nAll checks passed." if not failures else f"\n{failures} check(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
