# Memory probe — manual end-to-end test cases

Empirical checks that the two memories work and that their injection reads clearly to the
agent. Run each with `make probe MSG="…" [U=<id>]` (see `app/CLAUDE.md` → "Manual probe");
inspect the durable result with `make memories` (lists the `memories` collection, live +
soft-deleted). The probe prints, per run: the **injected long-term index** (grouped by
category, what the agent sees going in), the **tool calls** (name + args), and the **answer**.

Two memories under test:
- **Short-term** (`store_memory`, baski) — per-reply scratchpad, wiped after the turn. For
  task data (search results, tool output).
- **Long-term** (`remember`/`read_memory`/`forget`, this app) — durable owner facts; index
  always injected, bodies on demand by `public_id`; `forget` is a soft delete.

Two rules the cases below depend on:
- **Recall must use a *fresh* `U=`** (new conversation). In the same conversation the fact is
  still in the transcript, so the agent answers from there and never touches long-term memory —
  the recall path isn't exercised. A fresh `U=` has an empty transcript but the same global
  memory store.
- **A natural prompt beats an on-the-nose one.** "Remember that I love X" tests obedience;
  the real test is whether the agent *judges* to save/recall/update when a durable fact slips
  into ordinary conversation with no mention of memory at all. The natural cases (§N) are the
  stronger signal.

## Scripted cases (explicit, for a quick smoke check)

### S1 — save a preference + don't pollute long-term with task data
`Tell me the latest news and remember that I love chocolate`
- `remember(category=preference, source.kind=user, title~chocolate)` — exactly one.
- News gathered via `google_search`, answered in plain language — **not** saved to long-term.
- `make memories`: one live `preference/user` chocolate doc; nothing for the news.

### S2 — recall, realistic expectation (fresh `U=`)
`What sweets do I like?`
- The injected index shows the chocolate line. The agent answers "chocolate".
- It does **not** re-`remember`. It may answer with **zero** tool calls — when the title is
  self-contained ("Loves chocolate"), reading the body adds nothing, so skipping `read_memory`
  is correct, not a miss. `read_memory` is for teaser titles (next case), not every recall.

### S3 — when to extract the body (teaser title → `read_memory` fires)
Save first: `Remember my usual coffee order: a large oat-milk flat white, two extra espresso shots, no sugar, extra hot.`
Then, fresh `U=`: `How many espresso shots are in my usual coffee, and is it sweetened?`
- Index shows `… — Usual coffee order` (a teaser; the detail is in the body).
- The agent calls `read_memory(public_id)` for that one memory only (not others) and answers
  from the body ("two extra shots… no sugar"). This is the check that the agent knows *when*
  the title is not enough and the body must be loaded.

### S4 — supersede via forget + re-remember (fresh `U=`)
`Actually I don't love chocolate anymore — now I love nuts.`
- `forget(chocolate id)` then `remember(title~nuts)`.
- `make memories`: chocolate doc present but `deleted_at` set (soft delete); a new live nuts doc.
- Run again with another fresh `U=`: the index shows nuts, not chocolate.

### S5 — short-term only (no long-term write)
`Compare the population of Tokyo and Osaka`
- `remember` **not** called (research data, not an owner fact). `make memories` unchanged.

## §N — natural-conversation cases (the real judgment test)

No prompt mentions "remember", "forget", or "memory". Each slips a durable fact into an
ordinary request. Use a fresh `U=` per case so recall/contradiction rely on the index.

### N1 — implicit save
`I'm vegetarian — can you suggest a few quick dinner ideas for tonight?`
- The agent should, unprompted, `remember(preference, "vegetarian")` — a durable trait — and
  answer the dinner question without saving the (ephemeral) recipes.

### N2 — implicit recall (no mention of the stored fact)
With "vegetarian" stored, fresh `U=`:
`A friend wants to grab dinner tonight and suggested a steakhouse. What should I order?`
- The agent should fold in the vegetarian preference on its own (flag the mismatch, suggest veg
  options). May be **zero** tool calls — the index line alone carries it.

### N3 — implicit contradiction in passing
With "vegetarian" stored, fresh `U=`:
`Honestly I've started eating meat again over the past month, feeling much better for it.`
- The agent should infer the preference is now stale: `forget` the vegetarian doc and
  `remember` the update — without being told to. `make memories`: vegetarian soft-deleted, a
  new live doc for the change.

## What to inspect in the injected index

The index is grouped by category, one pointer line per live memory:
```
YOUR LONG-TERM MEMORY (grouped by category)…
PREFERENCE
- [public_id] source · date — title
FACT
- [public_id] source · date — title
```
Confirm: titles only (no bodies leak in); the date is present (the agent is told an old date
may be stale); soft-deleted memories are absent; the category appears once as a group header,
not repeated per line.
