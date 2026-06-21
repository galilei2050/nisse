# Memory probe — manual end-to-end test cases

Empirical checks that the two memories work and that their injection reads clearly to the
agent. Run each with `make probe MSG="…" [U=<id>]` (see `app/CLAUDE.md` → "Manual probe");
inspect the durable result with `make memories` (lists the `memories` collection, live +
soft-deleted). The probe prints, per run: the **injected long-term index** (grouped by
category, what the agent sees going in), the **tool calls** (name + args), and the **answer**.

Two memories under test:
- **Short-term** (`store_memory`, baski) — per-reply scratchpad, wiped after the turn. For
  task data (search results, tool output).
- **Long-term** (`remember`/`read_memory`/`edit_memory`/`forget`, this app) — durable owner facts;
  index always injected (source *kind* + `updated_at` per line); the body and the external source
  link (`source.ref`) come on demand by `public_id`. Correcting a memory is in place:
  `remember(public_id, …)` overwrites the whole record, `edit_memory` patches part of a long body
  (replace a fragment, or append when `old` is empty); `forget` is a soft delete, only for facts that
  no longer hold.

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

### S6 — refine a small record in place (overwrite, same id)
With "Loves chocolate" stored, fresh `U=`: `Actually, to be precise — I only love dark chocolate, 70% or higher.`
- `remember(public_id=<chocolate id>, …, title~dark chocolate)` — overwrites the whole record.
- `make memories`: **one** live doc, **same `public_id`**, body/title updated, `updated_at` bumped — no
  second doc, no soft-delete. The model should overwrite, not forget-then-re-add.

### S7 — extend a long body (edit_memory append, empty `old`)
With the S3 coffee order stored, fresh `U=`: `For my usual coffee, also note I want it in a ceramic cup, not paper.`
- `edit_memory(public_id=<coffee id>, old="", new="… ceramic cup, not paper")` — appends one line.
- `make memories`: the coffee body now ends with the ceramic-cup line; the rest of the order is intact
  (the point: it did **not** rewrite the whole body). Same doc, `updated_at` bumped.

### S8 — change part of a long body (edit_memory replace + no-match retry)
With the S3 coffee order stored, fresh `U=`: `Change my usual coffee to three espresso shots instead of two.`
- `edit_memory(public_id=<coffee id>, old~"two extra espresso shots", new~"three extra espresso shots")`
  — replaces only that fragment; the rest is untouched.
- If the first `old` doesn't match the body verbatim, the tool returns the current body unchanged and the
  agent retries `edit_memory` against that exact text — at most one extra call, never a silent no-op.

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
- The agent should infer the preference is now stale and correct it **in place** — without being told
  to — by overwriting the record: `remember(public_id=<vegetarian id>, category=preference, title~eats
  meat again)`. `make memories`: same `public_id`, now reflecting the change (forget-then-re-add is the
  worse path — it churns the id; only `forget` if the preference is dropped with nothing replacing it).

## What to inspect in the injected index

The index is grouped by category, one pointer line per live memory:
```
YOUR LONG-TERM MEMORY (grouped by category)…
PREFERENCE
- [public_id] source · date — title
FACT
- [public_id] source · date — title
```
Confirm: titles only (no bodies leak in); the date shown is `updated_at` — an overwritten or
edited memory shows its **edit** date, not its creation date, so a freshly-corrected fact doesn't
look stale (the agent is told an old date may be stale); soft-deleted memories are absent; the
category appears once as a group header, not repeated per line.
