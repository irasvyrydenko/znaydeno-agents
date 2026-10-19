"""Chat posts by id, and new posts from free-text requests.

pair_posts: the orchestrator shows the Verifier both posts of a pair.
ingest: a free-text request ("загубила зарядку в трапезній") is a new chat post. It is saved
to user_lost.csv or user_found.csv with the next free id, and the request then names that
post, so it goes the same way as "Допоможи знайти L03": Matcher, Verifier, confirm_match.

Agents never call this themselves: posting is what the chat does when a message arrives,
not an agent action, so the three-tool set stays as it is (see CLAUDE.md).
"""
from __future__ import annotations

import csv
import re
from datetime import datetime

from lostfound.agents.matcher import FOUND_ID, LOST_ID, detect_direction
from lostfound.server_v2 import POST_FIELDS, USER_POSTS, _read_posts

FILES = {"lost": "lost.csv", "found": "found.csv"}
PREFIX = {"lost": "L", "found": "F"}


def _by_id(filename: str) -> dict[str, dict[str, str]]:
    return {row["id"]: row for row in _read_posts(filename)}


def pair_posts(lost_id: str | None, found_id: str | None) -> dict[str, dict[str, str] | None]:
    """{"lost": row or None, "found": row or None}; None for an id that does not exist."""
    return {
        "lost": _by_id("lost.csv").get(lost_id or ""),
        "found": _by_id("found.csv").get(found_id or ""),
    }


def _next_id(direction: str) -> str:
    prefix = PREFIX[direction]
    numbers = [int(m.group(1)) for row in _read_posts(FILES[direction])
               if (m := re.fullmatch(rf"{prefix}(\d+)", row["id"]))]
    return f"{prefix}{max(numbers, default=0) + 1:02d}"


def add_post(direction: str, text: str) -> dict[str, str]:
    """Save a new open post written through the system and return it."""
    path = USER_POSTS[FILES[direction]]
    post = {
        "id": _next_id(direction),
        "posted_at": datetime.now().strftime("%Y-%m-%dT%H:%M"),
        "event_date": "",  # free text rarely gives a date the code can trust
        "author": "user_chat",
        "status": "open",
        "text": " ".join(text.split()),
    }
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=POST_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(post)
    return post


def ingest(request: str) -> tuple[str, dict[str, str] | None]:
    """A request that names no post becomes a new post; return (request naming it, the post).

    A request that already names a post (L.. or F..) is returned unchanged.
    """
    if LOST_ID.search(request) or FOUND_ID.search(request):
        return request, None
    post = add_post(detect_direction(request), request)
    return f"{post['id']}: {request}", post
