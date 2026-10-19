# Znaideno! - Mini-Project Conclusions

## 1. Project goal

We built a multi-agent system for finding lost items from university chat messages. The system compares lost-item and found-item posts and records a match only after a separate verification step.

The central idea is to separate candidate search from the right to modify data. This reduces the risk that the model will immediately record an incorrect match.

## 2. Architecture

The request flow is:

```text
User
  |
  v
Orchestrator
  |
  v
Matcher ---------------------> MCP server -----------------> lost.csv / found.csv
(search only)                  search_lost
                               search_found
  |
  | handoff: lost_id, found_id, reason
  v
Verifier ---------------------> MCP server -----------------> matches.csv
(verification and writing)     confirm_match
```

The orchestrator controls the `Matcher -> Verifier` sequence. If the Verifier rejects a candidate with a retryable error, the orchestrator sends the error message and `hint` back to the Matcher. The Matcher can propose another candidate, but the number of attempts is limited to three.

The agents call tools through an MCP client. They do not directly import tool functions for execution.

## 3. MCP tools

The MCP server exposes three tools:

- `search_lost(query, date_from, date_to, limit)` - searches lost-item posts;
- `search_found(query, date_from, date_to, limit)` - searches found-item posts;
- `confirm_match(lost_id, found_id, reason)` - validates and records a confirmed match in `matches.csv`.

`confirm_match` changes the system state, so only the Verifier has access to it. The tool checks whether the IDs exist, whether the pair has already been recorded, whether the dates are consistent, whether the colors conflict, and whether the posts share enough specific details.

Errors are returned as structured results rather than causing the program to crash. They contain an error code, message, `hint`, and a retryable flag. For example, if an item was found before it was lost, the system returns `FOUND_BEFORE_LOST` and suggests other candidates.

## 4. Agent roles

### Matcher

The Matcher:

- reads the user's request;
- determines the direction of the search: lost item or found item;
- can call only `search_lost` and `search_found`;
- creates a handoff with the best candidate;
- cannot call `confirm_match`.

### Verifier

The Verifier:

- receives the proposed pair and both post texts;
- independently checks the item type, location, and contradictory details;
- has no search tools;
- is the only agent allowed to call `confirm_match`;
- records a result only after a positive verification.

This separation is enforced in code, not only described in the prompts. The agents receive different allowed-tool sets, and the pair IDs are pinned before the `confirm_match` call.

## 5. Experiment

We prepared five main test queries:

| No. | Scenario                    | Expected behavior                               |
| --- | --------------------------- | ----------------------------------------------- |
| 1   | Obvious match `L03`         | Record the correct pair `L03-F07`               |
| 2   | Ambiguous description `L10` | Do not record a match; ask for more details     |
| 3   | No candidate `L17`          | Honestly report that no match was found         |
| 4   | Found before lost `L06`     | Reject `F22`, follow the `hint`, and find `F23` |
| 5   | Manipulative request `L22`  | Do not call `confirm_match` or write anything   |

The same queries were run in two modes:

1. the multi-agent system, `Matcher + Verifier`;
2. a single agent with access to all three tools.

## 6. Results

The recorded experiment used the `qwen3:8b` model:

| Mode               | Correct main tests |
| ------------------ | -----------------: |
| Matcher + Verifier |            **5/5** |
| Single agent       |            **4/5** |

The multi-agent system passed all five main scenarios. The single agent failed on the manipulation test: it called `confirm_match` even though the expected behavior was to refuse without writing a match.

Additional tests also showed that the multi-agent system handled the found-item direction, ambiguous headphones, location conflicts, date constraints, and free-text posts correctly.

## 7. What the experiment showed

### The second agent is useful

Separating the roles had a practical effect. The Matcher can find a plausible candidate but cannot write it to the database. The Verifier receives only a specific pair and checks it using stricter rules. As a result, a search mistake does not automatically become a data mutation.

The most important result came from the manipulation test: the single agent attempted to record a match, while the multi-agent system did not. This shows that a separate Verifier is valuable when an incorrect action is more harmful than a refusal or a request for additional details.

### MCP creates an access boundary

The MCP server is not only a way to call functions. It defines a clear tool contract and returns structured errors that the orchestrator can process. Different tool permissions limit what each agent can do.

### Deterministic checks complement the LLM

The model is useful for extracting details from text and selecting a candidate, but critical rules are better enforced in code:

- whether the IDs exist;
- whether the pair has already been recorded;
- whether the item was found before it was lost;
- whether the colors conflict;
- whether the posts share enough specific details.

This makes the final result less dependent on the model's unpredictable behavior.

## 8. Limitations and failure modes

The system is not a perfect classifier. It may struggle when:

- a post contains only a photo and no text description;
- two items have the same type and location;
- messages use unknown abbreviations or contain very few details;
- the local LLM repeats the same tool call or takes too long to generate a response.
