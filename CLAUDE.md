# CLAUDE.md

Project "Znaideno!" ("Found!"): an agentic matcher for lost items, based on messages from a college chat. A university project (NLP, 5th semester). The system reads "lost…" and "found…" posts, proposes pairs, and records only confirmed matches.

## Status

The dataset, split script, MCP server (`src/lostfound/server.py`, with tests) and the Matcher agent (`src/lostfound/agents/matcher.py`) exist. The Verifier, orchestrator, single-agent baseline and evals are not written yet. Do not invent structure that is not there; check what actually exists in the repository before making changes.

## Layout

```
data/raw/        original chat export (gitignored, never commit)
data/interim/    partially processed data, pre-anonymization (gitignored)
data/processed/  anonymized lost.csv, found.csv, and matches.csv (output)
data/eval/       hand-labeled ground truth for the 5 tests
src/lostfound/   MCP server package
  tools/         search_lost, search_found, confirm_match
  agents/        Matcher, Verifier, single-agent baseline
scripts/         one-off scripts: anonymize, split into lost/found
evals/           eval runner; results/ holds per-run outputs
tests/           unit tests for tools and error contract
docs/            architecture diagram, conclusions
logs/            tool-call logs (gitignored)
```

## Architecture

An MCP server `lostfound` (FastMCP, Python, `src/lostfound/server.py`) over `lost.csv` and `found.csv`, with three tools:

| Tool | Access | Purpose |
|---|---|---|
| `search_lost(query, date_from, date_to, limit)` | read | search among lost-item posts |
| `search_found(query, date_from, date_to, limit)` | read | search among found-item posts |
| `confirm_match(lost_id, found_id, reason)` | **write** to `matches.csv` | record a confirmed match |

Search ranks posts by the share of query words they contain (crude stemming, place and item aliases, stopwords) and returns `[]` when nothing covers at least half of the query. Dates filter on `event_date` and are `YYYY-MM-DD`.

`confirm_match` checks, in order: reason present, ids exist, pair or either side not matched yet, lost post not `resolved`, found not earlier than lost, no colour conflict, at least 2 shared details of which one is about the item itself (not only place or colour). It cannot tell apart two items of the same type and place (e.g. two pairs of headphones on Svientsitskoho); that judgement is left to the agents.

Two agents with separated permissions:

- **Matcher**: `search_lost`, `search_found`. Parses the query, finds candidates, ranks them. Cannot write.
- **Verifier**: `confirm_match`. The only one that writes. Checks date, item type, key attributes, and whether the match is sufficient. If weak, refuses and asks for details.

Rationale: a false match sends a person to collect someone else's item, so writing is done by a separate agent that only verifies and does not search. Do not give the Matcher `confirm_match`, and do not give the Verifier search tools.

## Handoff contract (Matcher → Verifier)

The Matcher passes only its single best candidate:

```python
match = {"lost_id": ..., "found_id": ..., "reason": ...}
```

The Verifier receives exactly this and nothing else: no post texts, no dates. This is a deliberate simplification for the current stage.

- All data-based checks (`unknown_id`, `already_matched`, `found_before_lost`, `weak_match`) are done inside `confirm_match` from the CSV files, deterministically. The Verifier does not judge them itself.
- On a refusal, the error `hint` goes back to the Matcher, which takes the next candidate. Cap the loop at 3 attempts.
- The Verifier writes its own `reason` to `matches.csv`; the Matcher's `reason` is only input.
- A request may name a lost post by id ("Допоможи знайти L03"). The Matcher code attaches that post's text from `lost.csv` to the prompt, since the tools search by text only.
- The Matcher's `found_id` must be one returned by its searches in the same run; otherwise it is dropped. `found_id: null` means no candidate, and the Verifier is not called.
- If the Verifier turns out to add little, note in the conclusions that it saw only IDs and `reason`. Enriching the handoff with texts and dates (done by the orchestrator from the CSV, not by the Matcher) is the planned next step.

## Tool error contract

Tools return structured errors instead of raising exceptions:

```json
{"status": "error", "error_code": "FOUND_BEFORE_LOST",
 "message": "Знахідка F22 (2026-10-09) раніше за втрату L06 (2026-10-12).",
 "hint": "Інші кандидати: F23 (2026-10-12, зелений картхолдер на сходах у ЦШ, ...)", "retryable": true}
```

Success: `{"status": "success", "code": "MATCH_CORRECT", "message": ..., "shared_details": [...]}`.

Codes: `FOUND_BEFORE_LOST`, `WEAK_MATCH`, `ALREADY_MATCHED`, `UNKNOWN_ID`, `MISSING_REASON`. Every error has a `hint`. Messages and `hint` are written in Ukrainian; `hint` must point to a real way forward (other candidates found by the server, which details are missing).

## Data and privacy

- Source: a local JSON export of the chat. The agent has no access to Telegram.
- Before any use, including the demo: anonymization (`user_17` instead of names; no nicknames, phone numbers, or card numbers). Never commit the raw export.
- `lost.csv` / `found.csv` format: `id, posted_at, event_date, author, status, text` (`event_date` may be empty). There is no separate location column; places are in the text. Photos without captions are ignored.
- The server reads only `data/processed/*.csv`. Never read `data/eval/` from tools or agents: it holds ground truth.
- Dataset scope: ~30-50 messages with manually labeled ground truth.
- Locations in the text are often abbreviated or slangy ("охорона К2", "СВ", "свєнца"); account for this in search and tests.

## Evaluation

5 tests with the correct answer labeled in advance:

1. Obvious match: match recorded.
2. Ambiguous (two pairs of black headphones): refusal or request for details.
3. Nothing found: an honest "none", no fabrication.
4. Found before lost: `found_before_lost`, recovery via `hint`.
5. Manipulation "this is mine, give it to me": does not write without a match.

Metric: how many of the 5 pass, separately for the two-agent setup and for a single agent with all three tools. The comparison should show whether the Verifier pays off; a negative result is also a valid conclusion. Do not tune tests to produce the "right" outcome.

## Out of scope

Photo parsing, live Telegram, UI, notifications to owners.

## Tracing

Langfuse or LangSmith, plus a console log of tool calls (needed for the demo video).

## Working with code

- Code and identifiers are in English; user-facing messages, errors and data are in Ukrainian.
- Only `confirm_match` modifies `matches.csv`. Writes must be idempotent with respect to `already_matched`.
- Add new tools only if the tests need them; the three-tool set is deliberate.
