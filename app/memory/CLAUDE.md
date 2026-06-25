# memory/ — recalled-on-demand long-term memory (the long tail)

Durable owner knowledge in Mongo `memories`, scoped per `conversation_id`. Tools: `recall_save` /
`recall_read` / `recall_edit` / `recall_forget`. Only a titled **index** is injected every turn (one pointer
line per memory); the **body** loads on demand via `recall_read(public_id)`.

This is one of TWO stores. Its sibling is **core memory** (`app/prompts/`, `update_core_memory`) — a
small block injected into the system prompt *every turn*. The split, and the rule for what goes
where, is the whole point of this design.

## The routing rule (why two stores)

The axis is **operational, not topical** — not "fact vs preference" (that word straddles and is why
the agent used to misfile). It is:

> **Core memory** = anything that must shape (almost) every reply even when the owner never brings it
> up. **Recall memory (here)** = anything that only matters once its own topic surfaces, where a
> short index title is enough to fetch it.
>
> Test the agent applies in one read: *"would ignoring this make me wrong on a turn that never
> mentions it?"* Yes → core; no → here.

Worked examples (these drove the design):
- "answer in feminine", "be concise" → **core** (behaviour, every turn).
- "I live in Santa Clara" → **core**, stored as the *operational projection* `timezone: America/Los_Angeles` (silently shapes time/scheduling/weather); the raw fact may stay here.
- "right now I'm focused on finding friends" → **core**, CURRENT FOCUS zone (transient, overwritten as life changes).
- "I love horror movies", "drives a BMW Z4", "flight on the 3rd" → **here** (`fact`/`event`): inert until films/cars/travel come up, and the title summons them then.

## Why it's built this way

- **Recall-on-demand is wrong for standing rules.** A behavioural rule the agent has to *remember to
  recall* is one it will violate — observed live: the owner said "answer in feminine", the agent
  `recall_read`'d an old preference, concluded "already saved", and never applied it. Standing rules
  must be always-on; that's core memory.
- **Always-on is expensive, so core is small and capped.** Every core char costs tokens on every
  turn. `update_core_memory` edits the block like a list (add/remove whole lines in one call, like
  `list_edit`; remove by exact line or unique fragment via shared `match_unique`) and is hard-capped
  (`_CORE_BUDGET`) on the *result*; when an add would overflow the agent must remove a less-relevant
  line in the same call. Editing line-by-line (never a wholesale rewrite) is what stops the model
  silently dropping rules — measured: across 20 overwrite-era updates the block churned 118 lines
  added / 104 removed-or-reworded, losing standing rules the owner never asked to touch. The cap forces
  keeping only what earns a slot (grounded in MemGPT/Letta: bounded, self-edited core blocks).
- **`preference` category was removed.** The word existed both as a memory category and as the core
  concept, so the agent couldn't tell an always-on rule from a recalled taste. `memories` now holds
  `fact` + `event` only; everything behavioural/identity/focus is core memory. The two stores no
  longer share a word, so routing is unambiguous.
- **A recall record is NOT a substitute for a core line.** The tools say so explicitly: don't skip
  writing to core because a similar memory exists, and don't search memory first. This kills the
  "already saved elsewhere → skip" failure.

Eviction (auto-demoting an evicted core line down into recall) is deliberately NOT built yet — the
cap just rejects oversize writes and the agent trims; revisit if the manual trim proves annoying.

## Tiers (≠ conversation transcript)

`MessageHistory` (baski) is the transcript/context window, not memory. `ShortTerm` (`working_note`)
is a per-reply scratchpad. `LongTerm` is this module. `Core memory` is `app/prompts/`.
