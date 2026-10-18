# CLAUDE.md

Project "Znaideno!" ("Found!"): an agentic matcher for lost items, based on messages from a college chat. A university project (NLP, 5th semester). The system reads "lost…" and "found…" posts, proposes pairs, and records only confirmed matches.

## Status

The dataset, split script, MCP server (`src/lostfound/server_v2.py`), the agents (`agents/matcher.py`, `agents/verifier.py`, `agents/orchestrator.py`, single-agent baseline `agents/single.py`), tracing (`src/lostfound/tracing.py`) and the eval runner (`evals/run_evals.py`) exist. Not run end to end against Ollama yet; no eval results or conclusions in `docs/` yet.

Run (needs Ollama with `LOSTFOUND_MODEL` pulled):

```
python -m lostfound.agents.orchestrator "Допоможи знайти L06"   # two agents, lost item
python -m lostfound.agents.orchestrator "Чиє це? F07"           # two agents, found item
python -m lostfound.agents.single "Допоможи знайти L06"         # baseline
python evals/run_evals.py [--mode multi|single|both] [--extra]   # 5 tests, results in evals/results/
pytest                                                           # no LLM needed
```

The orchestrator is plain code, not an LLM: Matcher → Verifier, on a retryable refusal the error and `hint` go back to the Matcher, at most 3 attempts. The Verifier's `confirm_match` ids are pinned to the handoff in code, and its verdict is read from the tool result, not from the model's text. The Matcher works in both directions: a lost item (`L..` or "загубила…") searches found posts with `search_found`; a found item (`F..`, "знайшла…", "чиє це?") searches lost posts with `search_lost`. The direction comes from the post id, otherwise from a keyword heuristic in `matcher.detect_direction`. Date guards mirror each other: lost → only `date_from = event_date`, found → only `date_to = event_date`. Do not invent structure that is not there; check what actually exists in the repository before making changes.

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

An MCP server `lostfound` (FastMCP, Python, `src/lostfound/server_v2.py`) over `lost.csv` and `found.csv`, with three tools:

`src/lostfound/server.py` is the first version, kept unchanged for comparison; agents and tests use `server_v2.py`. Its search falls back to the first rows when nothing matches, and its `WEAK_MATCH` is a Jaccard threshold of 0.05.

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

The Matcher passes only its single best candidate, in either direction (the orchestrator also gets a `direction` field and strips it before the Verifier):

```python
match = {"lost_id": ..., "found_id": ..., "reason": ...}
```

The Verifier receives this plus both posts of the pair (id, `event_date`, `status`, text), which the orchestrator reads from the CSV files (`agents/posts.py`). It never sees search results and has no search tools.

**Verifier** works in two steps (`agents/verifier.py`):
1. Check, LLM without tools: the model fills in a structured check from the two posts (item and place in each, quoted; `same_item`, `place_conflict`, `contradictions`). Code then decides, and re-checks the model where it is unreliable:
   - a place quote not found in its own post is dropped; no place conflict unless both posts name a place, and none when both name the same room number or spot;
   - a contradiction counts only if both posts state the attribute;
   - shared details are counted in code (server word matching): besides colour, at least two (the item and one more: brand, place, room). "чорні навушники" alone is `too_vague`.
   Declined `problem` = `different_item` / `contradiction` / `too_vague`. `ask_owner` names only kinds of details to ask for, never the found post's details (whoever reads them could claim the item).
2. Record, LLM with `confirm_match`: only after the check passes.

Why two steps: given the tool straight away, qwen3:8b called `confirm_match` for every pair, including ones that `confirm_match` itself accepts but ground truth marks wrong (L07–F15 different place, L19–F04 different room, L01–F01/F02 generic headphones). The Matcher's reason is not judged: an earlier "invented detail" check misfired on noise and refused right pairs; the Verifier compares the posts and writes its own reason instead.

- All data-based checks (`unknown_id`, `already_matched`, `found_before_lost`, `weak_match`) are done inside `confirm_match` from the CSV files, deterministically. The Verifier does not judge them itself.
- On a refusal, the error `hint` goes back to the Matcher, which takes the next candidate. Cap the loop at 3 attempts.
- The Verifier writes its own `reason` to `matches.csv`; the Matcher's `reason` is only input.
- A request may name a post by id ("Допоможи знайти L03", "Чиє це? F07"). The Matcher code attaches that post's text from `lost.csv` or `found.csv` to the prompt, since the tools search by text only.
- The candidate id (the `found_id` for a lost item, the `lost_id` for a found item) must be one returned by the Matcher's searches in the same run; otherwise it is dropped. A null candidate means no match, and the Verifier is not called.
- **Free-text requests** ("загубила зарядку в трапезній") are new chat posts. Before the Matcher runs, the orchestrator (code, not an agent) saves the message to `data/processed/user_lost.csv` or `user_found.csv` with the next free id (`L26`, `F33`, …, `event_date` empty, `author` `user_chat`) and rewrites the request as `L26: <text>` (`agents/posts.py`, `ingest`). From there it goes like "Допоможи знайти L03". The server reads the user files together with the dataset; they are gitignored, cleared by the eval runner before every test and by the UI reset button. The single-agent baseline ingests the same way. Posting is what the chat does when a message arrives, so no fourth tool was added.
- Refusal hints list alternatives from both sides: other found posts for the lost one, and other open lost posts for the found one.
- A Verifier refusal (`VERIFIER_DECLINED` with its `problem`) goes back to the Matcher like a tool refusal; `too_vague` counts as "needs details", like `WEAK_MATCH`.

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

Console log of tool calls (for the demo video), a local JSONL trace per process in `logs/trace-*.jsonl`, and Langfuse or LangSmith spans when their keys are set in `.env` (see `tracing.py`).

## Working with code

- Code and identifiers are in English; user-facing messages, errors and data are in Ukrainian.
- Only `confirm_match` modifies `matches.csv`. Writes must be idempotent with respect to `already_matched`.
- Add new tools only if the tests need them; the three-tool set is deliberate.
