"""FastMCP server for searching and confirming lost-and-found items.

Reads only data/processed/lost.csv and found.csv; confirm_match is the only writer
(data/processed/matches.csv). Never reads data/eval/.
"""

from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastmcp import FastMCP


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data" / "processed"
MATCHES_PATH = DATA_DIR / "matches.csv"
MATCH_FIELDS = ["lost_id", "found_id", "reason", "confirmed_at"]

# A post is returned by search only if it covers at least this share of the query words
MIN_QUERY_COVERAGE = 0.5
# Hints search with the whole lost post, which is long and chatty, so the bar is lower
HINT_QUERY_COVERAGE = 0.2
# confirm_match needs at least this many shared details, one of them about the item itself
MIN_SHARED_WORDS = 2

mcp = FastMCP("lostfound")


# --- text normalization ------------------------------------------------------

STOPWORDS = {
    # prepositions, conjunctions, particles
    "в", "у", "на", "з", "із", "зі", "до", "від", "біля", "без", "для", "під", "по", "за", "та", "і", "й",
    "а", "або", "чи", "що", "це", "там", "тут", "де", "як", "але", "якщо", "ще", "вже", "так", "не", "ні",
    "часом", "можливо", "десь", "такий", "така", "мій", "моя", "моє", "мої", "все", "всі", "будь", "ласка",
    "дуже", "поки", "зараз", "тепер", "лише",
    # chat filler and verbs that every post has
    "привіт", "хтось", "ніхто", "може", "бачив", "бачила", "бачили", "знаходив", "знаходила",
    "загубила", "загубив", "загубили", "забула", "забув", "забули", "лишила", "лишив", "лишилась",
    "залишила", "залишив", "залишили", "знайшла", "знайшов", "знайшли", "знайдена", "знайдений",
    "знайдено", "відніс", "віднесла", "віднесли", "віддав", "лежить", "лежала", "лежав", "напишіть",
    "пишіть", "заберіть", "вчора", "сьогодні", "допоможіть", "допоможи", "знайти", "річ",
    "знайдете", "побачите", "маякніть", "вдячна", "вдячний", "дякую", "була", "був", "нема", "немає",
    "її", "його", "він", "вона", "їх", "зара",
    # where items are handed in and generic room words: they say nothing about the item
    "охорона", "охорону", "охороні", "охоронця", "охоронцю", "охоронці", "охоронцем",
    "аудиторія", "аудиторії", "аудиторію", "пара", "парі", "пари",
}

# Different spellings of the same thing. Every word in a group is expanded to the whole
# group, so "СВ" in a query matches "Свєнціцького" in a post and vice versa.
ALIAS_GROUPS = [
    {"св", "свєнца", "свєнціцького", "свєнціцкого"},
    {"аірподс", "аірподси", "айрподс", "айрподси", "airpods", "прошки", "навушники", "навушник"},
    {"type", "тайпсі"},
    {"лайтнінг", "lightning", "айфона", "айфон"},
    {"перстень", "кільце", "каблучка"},
    {"парасолька", "парасоля"},
    {"пропуск", "перепустка"},
]
_ALIASES = {word: group for group in ALIAS_GROUPS for word in group}
PLACE_GROUP = ALIAS_GROUPS[0]

# Words about where, not what. A match built only from these (plus colours) is weak.
PLACE_WORDS = {"к1", "к2", "цш", "бфк", "ак", "ддс", "кампус", "кампусі"}
PLACE_STEMS = (
    "трапезн", "козельницьк", "палильн", "церкв", "лекторі", "світлиц", "поверс", "поверх",
    "сход", "дворик", "парк", "стрийськ", "храм", "кімнат", "столі", "столик", "підвікон",
    "дивані", "вішаку", "зупинк", "дороз",
)

COLOR_STEMS = [
    "чорн", "біл", "сір", "зелен", "червон", "син", "жовт", "коричнев", "рожев", "фіолетов",
    "бежев", "золот", "срібн", "блакитн", "помаранчев", "прозор",
]
_ADJ_ENDINGS = "ий|а|е|і|у|ого|ої|ому|ій|им|ою|их|ими|ю|о"
_COLOR_RE = re.compile(rf"^({'|'.join(COLOR_STEMS)})({_ADJ_ENDINGS})$")


def _tokens(text: str) -> list[str]:
    words = re.findall(r"\w+", text.lower().replace("’", "'").replace("ʼ", "'"))
    return [w for w in words if w not in STOPWORDS and (len(w) > 1 or w.isdigit())]


def _expand(words: list[str]) -> set[str]:
    expanded = set(words)
    for word in words:
        expanded |= _ALIASES.get(word, set())
    return expanded


def _same_word(a: str, b: str) -> bool:
    """Crude Ukrainian stemming: same word if they differ only in an ending of up to 3 letters.

    "поверх"/"поверсі" and "чорний"/"чорні" match; "гамак"/"гаманець" does not.
    """
    if a == b:
        return True
    if a.isdigit() or b.isdigit():
        return False
    prefix = 0
    for x, y in zip(a, b):
        if x != y:
            break
        prefix += 1
    return prefix >= 3 and len(a) - prefix <= 3 and len(b) - prefix <= 3


def _contains(word: str, text_words: set[str]) -> bool:
    variants = _ALIASES.get(word, {word})
    return any(_same_word(v, w) for v in variants for w in text_words)


def _shared_words(words: list[str], other_text: str) -> list[str]:
    """Words of `words` present in other_text; synonyms of one alias group count once."""
    other = _expand(_tokens(other_text))
    shared, groups = [], set()
    for word in dict.fromkeys(words):
        group = _ALIASES.get(word)
        key = id(group) if group else word
        if key not in groups and _contains(word, other):
            groups.add(key)
            shared.append(word)
    return shared


def _is_place(word: str) -> bool:
    return (
        word.isdigit()
        or word in PLACE_GROUP
        or word in PLACE_WORDS
        or word.startswith(PLACE_STEMS)
    )


def _is_color(word: str) -> bool:
    return bool(_COLOR_RE.match(word))


def _colors(text: str) -> set[str]:
    colors = set()
    for word in _tokens(text):
        match = _COLOR_RE.match(word)
        if match:
            colors.add(match.group(1))
    return colors


# --- data ----------------------------------------------------------------------


def _read_posts(filename: str) -> list[dict[str, str]]:
    with (DATA_DIR / filename).open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def _read_matches() -> list[dict[str, str]]:
    if not MATCHES_PATH.exists():
        return []
    with MATCHES_PATH.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def _short(row: dict[str, str]) -> str:
    return f"{row['id']} ({row['event_date'] or 'дата невідома'}, {row['text'][:60]})"


# --- search ----------------------------------------------------------------------


def _search(
    filename: str,
    query: str,
    date_from: Optional[str],
    date_to: Optional[str],
    limit: int,
    exclude: frozenset[str] = frozenset(),
    min_coverage: float = MIN_QUERY_COVERAGE,
) -> list[dict[str, Any]]:
    rows = [row for row in _read_posts(filename) if row["id"] not in exclude]
    if date_from:
        rows = [row for row in rows if row["event_date"] and row["event_date"] >= date_from]
    if date_to:
        rows = [row for row in rows if row["event_date"] and row["event_date"] <= date_to]

    terms = list(dict.fromkeys(_tokens(query)))
    if not terms:
        return []

    # Same rule as confirm_match: if the query names an item, a post sharing only the place or
    # colour is not a candidate ("цноту 242 Свєнціцького" must not return a lost library card in 242)
    names_item = any(not _is_place(t) and not _is_color(t) for t in terms)

    scored = []
    for row in rows:
        shared = _shared_words(terms, row["text"])
        if names_item and all(_is_place(w) or _is_color(w) for w in shared):
            continue
        coverage = len(shared) / len(terms)
        if coverage >= min_coverage:
            scored.append({**row, "score": round(coverage, 2)})

    # Best coverage first; among equals, the most recent event first
    scored.sort(key=lambda row: (row["score"], row["event_date"]), reverse=True)
    return scored[: max(0, limit)]


@mcp.tool
def search_found(
    query: str, date_from: Optional[str] = None, date_to: Optional[str] = None, limit: int = 5
) -> list[dict[str, Any]]:
    """Search found-item posts by text and an inclusive event-date range (YYYY-MM-DD).

    Returns the best matches first, each with a score (share of query words found).
    An empty list means nothing matched.
    """
    return _search("found.csv", query, date_from, date_to, limit)


@mcp.tool
def search_lost(
    query: str, date_from: Optional[str] = None, date_to: Optional[str] = None, limit: int = 5
) -> list[dict[str, Any]]:
    """Search lost-item posts by text and an inclusive event-date range (YYYY-MM-DD).

    Returns the best matches first, each with a score (share of query words found).
    An empty list means nothing matched.
    """
    return _search("lost.csv", query, date_from, date_to, limit)


# --- confirm_match ---------------------------------------------------------------


def _error(error_code: str, message: str, retryable: bool, hint: str) -> dict[str, Any]:
    return {
        "status": "error",
        "error_code": error_code,
        "message": message,
        "retryable": retryable,
        "hint": hint,
    }


def _other_candidates(lost: dict[str, str], found: dict[str, str]) -> str:
    """Real alternatives for a hint, from both sides of the rejected pair:
    found posts similar to the lost one (not earlier than it), and open lost posts similar
    to the found one (not later than it). The Matcher uses whichever side it is searching."""
    matches = _read_matches()
    taken_found = {row["found_id"] for row in matches}
    taken_lost = {row["lost_id"] for row in matches}
    resolved = {row["id"] for row in _read_posts("lost.csv") if row["status"] == "resolved"}
    found_alternatives = _search(
        "found.csv", lost["text"], lost["event_date"] or None, None, 3,
        frozenset({found["id"]} | taken_found), HINT_QUERY_COVERAGE,
    )
    # Found posts are short, so the normal search bar applies; the low hint bar only matches places
    lost_alternatives = _search(
        "lost.csv", found["text"], None, found["event_date"] or None, 3,
        frozenset({lost["id"]} | taken_lost | resolved), MIN_QUERY_COVERAGE,
    )
    if not found_alternatives and not lost_alternatives:
        return "Інших кандидатів не знайдено; попросіть у власника більше деталей."
    parts = []
    if found_alternatives:
        parts.append("Інші кандидати: " + "; ".join(_short(row) for row in found_alternatives))
    if lost_alternatives:
        parts.append(
            f"Інші втрати, схожі на {found['id']}: " + "; ".join(_short(row) for row in lost_alternatives)
        )
    return ". ".join(parts)


@mcp.tool
def confirm_match(lost_id: str, found_id: str, reason: str) -> dict[str, Any]:
    """Validate and persist a confirmed lost/found pair.

    Checks, in order: reason present, ids exist, not matched yet, lost post still open,
    found not earlier than lost, same item (enough shared details, no colour conflict).
    Writes to matches.csv only if every check passes.
    """
    if not reason or len(reason.strip()) < 10:
        return _error(
            "MISSING_REASON",
            "Аргументація збігу порожня або занадто коротка.",
            retryable=True,
            hint="Поясніть, які ознаки збігаються: тип речі, колір, місце, дата.",
        )

    lost_items = {row["id"]: row for row in _read_posts("lost.csv")}
    found_items = {row["id"]: row for row in _read_posts("found.csv")}
    lost = lost_items.get(lost_id)
    found = found_items.get(found_id)
    if lost is None or found is None:
        unknown = ", ".join(i for i, row in ((lost_id, lost), (found_id, found)) if row is None)
        return _error(
            "UNKNOWN_ID",
            f"Записів з ID {unknown} не існує.",
            retryable=True,
            hint="Використовуйте лише id з результатів search_lost (L..) і search_found (F..).",
        )

    for row in _read_matches():
        if row["lost_id"] == lost_id and row["found_id"] == found_id:
            return _error(
                "ALREADY_MATCHED",
                f"Пару {lost_id} — {found_id} уже записано.",
                retryable=False,
                hint="Нічого робити не потрібно: збіг уже збережено.",
            )
        if row["lost_id"] == lost_id or row["found_id"] == found_id:
            return _error(
                "ALREADY_MATCHED",
                f"{lost_id if row['lost_id'] == lost_id else found_id} уже зіставлено в парі "
                f"{row['lost_id']} — {row['found_id']}.",
                retryable=False,
                hint="Ця річ уже має власника або знахідку; шукайте інших кандидатів.",
            )
    if lost["status"] == "resolved":
        return _error(
            "ALREADY_MATCHED",
            f"Оголошення {lost_id} закрите (status=resolved): річ уже повернули.",
            retryable=False,
            hint="Для закритих оголошень збіг не записується.",
        )

    lost_date, found_date = lost["event_date"], found["event_date"]
    if found_date and lost_date and found_date < lost_date:
        return _error(
            "FOUND_BEFORE_LOST",
            f"Знахідка {found_id} ({found_date}) раніше за втрату {lost_id} ({lost_date}).",
            retryable=True,
            hint=_other_candidates(lost, found),
        )

    lost_colors, found_colors = _colors(lost["text"]), _colors(found["text"])
    if lost_colors and found_colors and not lost_colors & found_colors:
        return _error(
            "WEAK_MATCH",
            f"Кольори не збігаються: у {lost_id} {', '.join(sorted(lost_colors))}*, "
            f"у {found_id} {', '.join(sorted(found_colors))}*.",
            retryable=True,
            hint=_other_candidates(lost, found),
        )

    shared = _shared_words(_tokens(lost["text"]), found["text"])
    item_words = [w for w in shared if not _is_place(w) and not _is_color(w)]
    if len(shared) < MIN_SHARED_WORDS or not item_words:
        if not shared:
            details = "спільних ознак немає"
        elif not item_words:
            details = f"спільні лише місце чи колір: {', '.join(shared)}"
        else:
            details = f"спільне лише: {', '.join(shared)}"
        return _error(
            "WEAK_MATCH",
            f"Оголошення {lost_id} і {found_id} майже не мають спільних деталей ({details}).",
            retryable=True,
            hint="Запитайте у власника колір, бренд чи особливі прикмети. "
            + _other_candidates(lost, found),
        )

    write_header = not MATCHES_PATH.exists() or MATCHES_PATH.stat().st_size == 0
    with MATCHES_PATH.open("a", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=MATCH_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow({
            "lost_id": lost_id,
            "found_id": found_id,
            "reason": reason.strip(),
            "confirmed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        })

    return {
        "status": "success",
        "code": "MATCH_CORRECT",
        "message": f"Збіг {lost_id} — {found_id} підтверджено та збережено.",
        "shared_details": shared,
    }


if __name__ == "__main__":
    mcp.run()
