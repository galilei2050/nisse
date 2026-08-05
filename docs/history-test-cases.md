# Conversation-history test cases

Expectation-first scenarios for the `conversation_turns` history + pruning logic
(`app/assistant/history.py`). Companion to `docs/memory-test-cases.md`.

**Running these is part of development.** When you change history/pruning, run every scenario
and add a new one for any behavior the existing set doesn't cover. Each scenario is driven by
`make probe` and verified against MongoDB with `make turns`.

## How to run

```
make probe U=<id> MSG="..."     # drive one reply (real API/DB; throwaway U=)
make turns U=<id>               # dump that conversation's turns: active + soft-deleted
```

Write the expected Mongo end-state **before** running; then compare.

## Behavior under test

- Each agentic turn = one document in `conversation_turns`, keyed by `(conversation_id, turn_id)` (unique).
- **Write-on-commit:** each turn is written to Mongo the moment it completes (`__exit__` fires a
  fire-and-forget task). A pure tool turn (messages are only `tool_use`/`tool_result`, no text) is
  written **already soft-deleted** (`deleted_at` set); `compact()`'s first cut then removes it from
  the active in-memory transcript so the next reply's context stays lean.
- **`flush()`** (called after the answer is sent, under the conversation lock) awaits the in-flight
  writes, then soft-deletes turns dropped by `compact()`/`delete_messages` — so trimming is durable
  and dropped turns don't resurrect on the next `load()`.
- **One method shrinks the transcript: `compact()`**, once per reply, after the agent loop has
  finished (`truncate()`, which the loop calls after every API call, only records the context size).
  A turn dropped mid-loop would both shrink the context a reply is still composing against and move
  the head of the message list, invalidating the whole cached prefix.
- **Its three cuts, in order of what the loss costs:** a turn with no text always goes → *over
  budget*, turns past `_PAYLOAD_RETENTION` keep their text but give up tool calls, results,
  attachments and reasoning → *still over budget*, whole turns go, oldest first. A tool call and its
  result live in the same turn, so a stripped turn never has a `tool_use` without its `tool_result`.
- **Nothing costly is cut on a guess.** Both budget-gated cuts need a measurement that actually
  crossed the threshold, and `load()` restores turns whole — so a small conversation keeps its
  attachments however old, and a restart doesn't change what the model can see.
- **The size counter is cleared by a cut** (`_last_input_tokens = 0`), because it described the
  transcript as it was before. Otherwise `context_status()` reports a fullness that no longer exists
  and the agent reads it as an instruction to prune.
- **Mongo holds the turn as it happened, always.** Compaction edits the in-memory transcript only, and
  the durable write is handed a snapshot taken when the turn completed — so a turn stripped in memory
  before its own fire-and-forget write lands still reaches Mongo whole. Nothing but `deleted_at` and
  `message_ids` ever changes on a stored turn.
- **Turns leave through one door.** `_forget()` is the only thing that removes a turn from the active
  transcript — compaction and the agent's `prune_transcript` (`delete_turns`) both go through it, so
  every removal is soft-deleted on `flush()` the same way and carries a `ForgetReason` in the log.
- **Kept active:** user questions, assistant answers, and narrated tool turns (a tool call that
  also carries assistant text).
- **Soft-deleted:** pure tool turns + truncated/deleted turns. Their full documents stay in Mongo —
  recoverable.
- Turn **content is written once and never modified** — only `deleted_at` changes after insert.
- `turn_id` is a sequential int from baski; `load()` advances the counter past **every** turn
  (including soft-deleted) so an id is never reused → no unique-index collision.
- Durable facts the agent wants later go to long-term memory (injected separately), not history.
- **Recency marker:** each `[Turn N]` marker carries the turn's absolute UTC send-time on the first
  (oldest visible) turn and after a >1h gap from the previous turn — `[Turn 8 · 2026-06-21 21:42 UTC]`;
  consecutive turns inside a session stay bare `[Turn N]`. Absolute UTC (never relative) so the marker
  is byte-stable in the cached prefix; the model derives "how long ago" from it plus the live
  current-time line in the non-cached tail. Send-time rides on `MongoTurn.created_at` (from `__enter__`
  live, from the Mongo doc on `load()`), normalized through baski `as_utc`.

> The unit tests in `tests/assistant/test_history.py` cover these invariants (write-once, durable
> trim, durable delete, recoverable pure-tool prune) against a fake collection. The probe
> scenarios below were last run against the **pre-rewrite** `save()` design — re-run them on the
> next live probe to confirm end-to-end against real Mongo.

**Note on model behavior:** Opus tends to *narrate* its tool calls ("I'll search for that…"), which
puts a text block in the tool turn — so that turn is KEPT whole (with its tool payload). Pure tool
turns (soft-deleted) arise when the model uses a thinking block instead of narration. So whether a
given run produces a soft-deleted turn is not fully deterministic; the **invariants** below hold in
every run regardless (no active turn is ever tool-only; soft-deleted turns keep full content).

## Last run (2026-06-20) — all 8 PASS

| # | Scenario | Result |
|---|----------|--------|
| 1 | basic-prune | PASS — pure tool turn soft-deleted, Q+A active (U=770011) |
| 2 | cross-message-context | PASS — follow-up answered "Ruffles" from loaded transcript |
| 3 | no-tool-case | PASS — 2 turns, both active, 0 soft-deleted |
| 4 | turn-id-uniqueness | PASS — 7 turns across 3 reloads, ids unique & monotonic |
| 5 | recoverability | PASS — soft-deleted turn: deleted_at set, full 1367-char tool_result intact |
| 6 | narrated-tool-turn-kept | PASS — text+tool turn kept active whole |
| 7 | multi-tool-one-reply | PASS — narrated multi-tool turn kept whole with both results |
| 8 | context-dependent-follow-up | PASS — "the first one" resolved to the specific concert |

---

## Scenario 1 — basic-prune

**Risk:** `_has_text` misclassifies a block, so a pure tool turn stays active (context bloat) or a
text turn is pruned (lost context).

**Steps:**
```
make probe U=770001 MSG="What's the current weather in Paris right now?"
make turns  U=770001
```
(A live query is required — a well-known fact like "capital of France" is answered with no tool,
so nothing is pruned. Force a tool call to exercise the prune path.)

**Expected Mongo end-state:** user-question turn active; the `google_search` tool round (no
narration text) soft-deleted; assistant-answer turn active. Every turn present.

**Pass:** `make turns` → active = user + answer; any pure-tool turn is `DELETED` and still shows
its `tool_use`/`tool_result` blocks; no active turn is tool-only.

---

## Scenario 2 — cross-message-context

**Risk:** `load()` fails to restore the prior transcript from Mongo, so a follow-up loses context.

**Steps:**
```
make probe U=770002 MSG="My dog is named Ruffles"
make probe U=770002 MSG="What is my dog's name?"
make turns  U=770002
```

**Expected Mongo end-state:** both exchanges' user + assistant turns active; second reply answers
"Ruffles" from the loaded transcript (no search).

**Pass:** second probe's answer contains "Ruffles"; `make turns` shows ascending `turn_id`, no gaps,
no duplicates, all four conversational turns active.

---

## Scenario 3 — no-tool-case

**Risk:** A pure-conversation turn is wrongly pruned (e.g. the `isinstance(content, str)` branch for
a user message, or the `TextBlock` branch for the answer, misfires).

**Steps:**
```
make probe U=770003 MSG="Tell me a short joke"
make turns  U=770003
```

**Expected Mongo end-state:** exactly 2 turns (user + assistant), both active, nothing soft-deleted.

**Pass:** `make turns` → `total=2 active=2 soft-deleted=0`; both turns show a `text:` block.

---

## Scenario 4 — turn-id-uniqueness

**Risk:** `_next_turn_id` restored from active turns only would reissue a soft-deleted turn's id and
collide on the unique index. The counter must advance past **all** turns.

**Steps:**
```
make probe U=770004 MSG="Search for news about Mars"
make probe U=770004 MSG="What was the most interesting result?"
make probe U=770004 MSG="Thanks, that's all"
make turns  U=770004
```

**Expected Mongo end-state:** all turns present; `turn_id`s strictly increasing and unique; highest
active id is below none of the soft-deleted ids that come after it — no reuse; no duplicate-key error.

**Pass:** all three probes succeed (no Mongo duplicate-key error); `make turns` shows every `turn_id`
unique and monotonic; no soft-deleted id equals an active id.

---

## Scenario 5 — recoverability

**Risk:** A pruned turn is hard-deleted or its `messages` corrupted/emptied on soft-delete — present
but unrestorable.

**Steps:**
```
make probe U=770005 MSG="Find me a Python job in Berlin"
make turns  U=770005
```

**Expected Mongo end-state:** the soft-deleted tool turn is still in Mongo with `deleted_at` set and
its full `tool_use` (with input) + `tool_result` (with payload) intact.

**Pass:** `make turns` → `soft-deleted ≥ 1`; each `DELETED` turn prints its `tool_use:`/`tool_result:`
blocks (non-empty); `total = active + soft-deleted` (nothing physically gone).

---

## Scenario 6 — narrated-tool-turn-kept

**Risk:** A turn with **both** a `TextBlock` and `tool_use`/`tool_result` (narrated tool call) is
mistaken for a pure tool turn and pruned — losing the narration and its tool payload. Exercises the
`isinstance(block, TextBlock)` vs dict-`type:"text"` distinction.

**Steps:**
```
make probe U=770006 MSG="Search for the best pizza in Naples and tell me what you find as you go"
make turns  U=770006
```

**Expected Mongo end-state:** any turn carrying both a text block and tool blocks is active (kept
whole, with its `tool_result`); pure tool turns (no text) soft-deleted.

**Pass:** `make turns` shows the narrated turn `active` with both `text:` and `tool_use:`/`tool_result:`
labels; no active turn is tool-only.

---

## Scenario 7 — multi-tool-one-reply

**Risk:** When several tool rounds happen in one reply, only some pure-tool turns are pruned (stale
payloads linger) or the wrong turn is pruned.

**Steps:**
```
make probe U=770007 MSG="Compare the current weather in London and Tokyo"
make turns  U=770007
```

**Expected Mongo end-state:** every pure-tool round soft-deleted; user-question turn and the final
comparison-answer turn active; all turns present.

**Pass:** `make turns` → `soft-deleted ≥ 2`, each tool-only; active = user + final answer; answer text
mentions both cities.

---

## Scenario 8 — context-dependent-follow-up (owner workflow)

**Risk:** A realistic follow-up using an implicit reference ("the first one") needs the first reply's
answer turn kept active and correctly round-tripped through Mongo; if pruned or malformed the agent
can't resolve the reference.

**Steps:**
```
make probe U=770008 MSG="Search for upcoming concerts in San Francisco this weekend"
make probe U=770008 MSG="How much are tickets for the first one?"
make turns  U=770008
```

**Expected Mongo end-state:** first reply's answer turn (the concert list) active after the second
reply; both replies' tool turns soft-deleted; turn_ids unique and monotonic across both.

**Pass:** second probe's answer names a specific event/price from the first answer (not "which
concert?"); `make turns` shows the first answer turn still `active`, no id collision.

---

## Scenario 9 — recency marker (time awareness)

**Risk:** the `[Turn N]` marker shows the wrong send-time, a relative string (breaks caching), or a
time on every turn (token noise) instead of only on the first turn and after a >1h gap.

**Steps:** reuse a conversation that already has turns from a prior session (>1h ago), then probe it.
```
make probe U=<existing-prior-day-id> MSG="что мы обсуждали?"
```
Inspect the probe's injected context (the `[Turn N …]` lines).

**Expected:** the first (oldest visible) turn carries `[Turn 1 · YYYY-MM-DD HH:MM UTC]`; consecutive
turns inside the old session are bare `[Turn N]`; the new turn (>1h later) carries its own UTC time.
Time is absolute UTC, never relative.

**Pass:** markers match the above; verified 2026-06-21 (U=770004 → turn 1 stamped 06-20, 2–7 bare,
turn 8 stamped 06-21). Unit-tested in `test_turn_marker_*` against the cold-start `load()` path.
