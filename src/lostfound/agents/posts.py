"""Look up chat posts by id. Used by the orchestrator to show the Verifier both posts of a pair.

Agents never call this themselves: the Matcher only has text search, and the Verifier gets
the posts from the orchestrator (see CLAUDE.md, "Handoff contract").
"""
from __future__ import annotations

import csv

from lostfound.server_v2 import DATA_DIR


def _read(filename: str) -> dict[str, dict[str, str]]:
    with (DATA_DIR / filename).open(encoding="utf-8", newline="") as file:
        return {row["id"]: row for row in csv.DictReader(file)}


def pair_posts(lost_id: str | None, found_id: str | None) -> dict[str, dict[str, str] | None]:
    """{"lost": row or None, "found": row or None}; None for an id that does not exist."""
    return {
        "lost": _read("lost.csv").get(lost_id or ""),
        "found": _read("found.csv").get(found_id or ""),
    }
