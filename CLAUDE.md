# CLAUDE.md

Project "Znaideno!" ("Found!"): an agentic matcher for lost items, based on messages from a college chat. A university project (NLP, 5th semester). The system reads "lost…" and "found…" posts, proposes pairs, and records only confirmed matches.

## Status

Early stage: only the proposal exists. No code, dataset or tests yet. Do not invent structure that is not there; check what actually exists in the repository before making changes.

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

An MCP server `lostfound` (FastMCP, Python) over `lost.csv` and `found.csv`, with three tools:

| Tool | Access | Purpose |
|---|---|---|
| `search_lost(text, since)` | read | search among lost-item posts |
| `search_found(text, since)` | read | search among found-item posts |
| `confirm_match(lost_id, found_id, reason)` | **write** to `matches.csv` | record a confirmed match |

Two agents with separated permissions:

- **Matcher**: `search_lost`, `search_found`. Parses the query, finds candidates, ranks them. Cannot write.
- **Verifier**: `confirm_match`. The only one that writes. Checks date, item type, key attributes, and whether the match is sufficient. If weak, refuses and asks for details.

Rationale: a false match sends a person to collect someone else's item, so writing is done by a separate agent that only verifies and does not search. Do not give the Matcher `confirm_match`, and do not give the Verifier search tools.

## Tool error contract

Tools return structured errors instead of raising exceptions:

```json
{"error": "found_before_lost", "message": "Знахідка 12.10, втрата 14.10.",
 "hint": "Інші кандидати: found #31 (14.10, охорона К2)", "retryable": true}
```

Codes: `found_before_lost`, `weak_match`, `already_matched`, `unknown_id`, `missing_reason`. Messages and `hint` are written in Ukrainian; `hint` must point to a real way forward (another candidate, which details are missing).

## Data and privacy

- Source: a local JSON export of the chat. The agent has no access to Telegram.
- Before any use, including the demo: anonymization (`user_17` instead of names; no nicknames, phone numbers, or card numbers). Never commit the raw export.
- `lost.csv` / `found.csv` format: `id, date, text, location` (location optional). Photos without captions are ignored.
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
