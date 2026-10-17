"""FastMCP server for searching and confirming lost-and-found items."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Optional, Any

from fastmcp import FastMCP


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data" / "processed"
MATCHES_PATH = DATA_DIR / "matches.csv"

mcp = FastMCP("lostfound")


def _read_posts(filename: str) -> list[dict[str, str]]:
    with (DATA_DIR / filename).open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def _search(
    filename: str,
    query: str,
    date_from: Optional[str],
    date_to: Optional[str],
    limit: int,
) -> list[dict[str, str]]:
    rows = _read_posts(filename)
    if date_from:
        rows = [row for row in rows if row["event_date"] and row["event_date"] >= date_from]
    if date_to:
        rows = [row for row in rows if row["event_date"] and row["event_date"] <= date_to]

    terms = query.lower().split()
    if terms:
        matches = [
            row
            for row in rows
            if all(term in row.get("text", "").lower() for term in terms)
        ]
        if matches:
            rows = matches

    return rows[: max(0, limit)]


@mcp.tool
def search_found(
    query: str, date_from: Optional[str] = None, date_to: Optional[str] = None, limit: int = 5
) -> list[dict[str, str]]:
    """Search found-item posts by text and an inclusive event-date range."""
    return _search("found.csv", query, date_from, date_to, limit)


@mcp.tool
def search_lost(
    query: str, date_from: Optional[str] = None, date_to: Optional[str] = None, limit: int = 5
) -> list[dict[str, str]]:
    """Search lost-item posts by text and an inclusive event-date range."""
    return _search("lost.csv", query, date_from, date_to, limit)


def _error(error_code: str, message: str, retryable: bool, **extra: str) -> dict[str, object]:
    return {
        "status": "error",
        "error_code": error_code,
        "message": message,
        "retryable": retryable,
        **extra,
    }

def _jaccard_similarity(text1: str, text2: str) -> float:
    """Обчислює математичний коефіцієнт схожості двох текстів (від 0.0 до 1.0)."""
    words1 = set(text1.lower().split())
    words2 = set(text2.lower().split())
    intersection = words1.intersection(words2)
    union = words1.union(words2)
    return len(intersection) / len(union) if union else 0.0


@mcp.tool
def confirm_match(lost_id: str, found_id: str, reason: str) -> dict[str, Any]:
    """Validate and persist a confirmed lost/found pair based on strict factual rules."""
    
    # 1. Схема: Перевірка наявності обґрунтування
    if not reason or len(reason.strip()) < 10:
        return _error(
            "MISSING_REASON",
            "Аргументація збігу порожня або занадто коротка.",
            retryable=True,
            hint="Надайте детальне пояснення, чому ці дві речі є однією і тією ж."
        )

    # 2. Перевірка існування ID
    lost_items = {row["id"]: row for row in _read_posts("lost.csv")}
    found_items = {row["id"]: row for row in _read_posts("found.csv")}
    lost_item = lost_items.get(lost_id)
    found_item = found_items.get(found_id)

    if lost_item is None or found_item is None:
        return _error("UNKNOWN_ID", f"Запис з ID {lost_id} або {found_id} не знайдено.", retryable=False)

    # 3. Перевірка повторного збігу в БД
    with MATCHES_PATH.open(encoding="utf-8", newline="") as file:
        matches = list(csv.DictReader(file))
    if any(row.get("lost_id") == lost_id or row.get("found_id") == found_id for row in matches):
        return _error("ALREADY_MATCHED", "Ця річ уже була зметчена раніше.", retryable=False)

    # 4. Перевірка часової логіки (Дати)
    lost_date = lost_item.get("event_date", "")
    found_date = found_item.get("event_date", "")
    if found_date and lost_date and found_date < lost_date:
        return _error(
            "FOUND_BEFORE_LOST",
            f"Знахідка {found_id} ({found_date}) раніше за втрату {lost_id} ({lost_date}).",
            retryable=True,
            hint=f"Шукайте у found.csv записи з датою event_date >= {lost_date}"
        )

    # 5. Алгоритмічна перевірка схожості продуктів (Jaccard Similarity)
    similarity = _jaccard_similarity(lost_item.get("text", ""), found_item.get("text", ""))
    if similarity < 0.05:
        return _error(
            "WEAK_MATCH",
            f"Тексти оголошень {lost_id} та {found_id} майже не мають спільних деталей.",
            retryable=True,
            hint="Запитайте у користувача уточнюючі прикмети або виберіть іншого кандидата."
        )

    # УСПІШНИЙ ЗАПИС У БД
    with MATCHES_PATH.open("a", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["lost_id", "found_id", "reason", "confirmed_at"])
        writer.writerow({
            "lost_id": lost_id,
            "found_id": found_id,
            "reason": reason,
            "confirmed_at": datetime.now().astimezone().isoformat(),
        })

    return {
        "status": "success",
        "code": "MATCH_CORRECT",
        "message": f"Збіг {lost_id} <-> {found_id} підтверджено та збережено."
    }


if __name__ == "__main__":
    mcp.run()