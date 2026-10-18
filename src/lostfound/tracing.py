"""Tracing: a local JSONL trace of every agent step and tool call, plus Langfuse or LangSmith
when configured in .env.

- Local: always on. One file per process in logs/trace-<timestamp>.jsonl.
- Langfuse: on when LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set (pip install -e ".[langfuse]").
- LangSmith: on when LANGSMITH_TRACING=true (pip install -e ".[langsmith]").
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("lostfound.tracing")

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "logs"
TRACE_PATH = LOG_DIR / f"trace-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"


def _backend() -> str | None:
    if os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"):
        return "langfuse"
    if os.getenv("LANGSMITH_TRACING", "").lower() == "true":
        return "langsmith"
    return None


BACKEND = _backend()


def _observe(name: str):
    if BACKEND == "langfuse":
        try:
            from langfuse import observe  # v3
        except ImportError:
            from langfuse.decorators import observe  # v2
        return observe(name=name)
    if BACKEND == "langsmith":
        from langsmith import traceable
        return traceable(name=name)
    return None


def traced(name: str):
    """Decorator: a span in Langfuse/LangSmith if configured, otherwise a no-op."""

    def decorator(fn):
        try:
            observe = _observe(name)
        except ImportError:
            log.warning("tracing backend %s is configured but not installed", BACKEND)
            return fn
        return observe(fn) if observe else fn

    return decorator


def record(event: str, **data: Any) -> None:
    """Append one event to the local JSONL trace."""
    LOG_DIR.mkdir(exist_ok=True)
    line = {"ts": time.time(), "event": event, **data}
    with TRACE_PATH.open("a", encoding="utf-8") as file:
        file.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")


def flush() -> None:
    """Send buffered spans before the process exits."""
    if BACKEND == "langfuse":
        try:
            from langfuse import get_client  # v3
            get_client().flush()
        except ImportError:
            from langfuse.decorators import langfuse_context  # v2
            langfuse_context.flush()
