# lists/ — the ARTIFACT tier (named mutable lists)

Shopping, todo, watchlist, packing — collections the owner **adds to and crosses off** over time.
Mongo `lists`, scoped per `conversation_id`. **Two tools on purpose:** `list_edit` (add/remove/clear
in one call) mutates; `list_show` reads + injects the always-present index (list names + counts) via
`user_message()`. The four CRUD verbs collapse into one mutation tool because every tool's schema
rides the context window on every turn — measured 4 verbs = 913 tok/turn, 2 = 645 (saves ~268/turn).
A pure read stays its own tool so it never carries a side effect.

Existing shopping/contradiction lists that had been mis-stored in long-term memory were moved here by
`scripts/migrate_lists.py` (one-off, idempotent: parses bullet/numbered/comma bodies, soft-deletes
the source memory).

## Why this is NOT memory

A list is an **artifact**, not a `fact`/`event`. It is mutable and has no single durable truth — its
whole nature is to change. Long-term memory (`app/memory`) is for inert facts recalled by topic;
putting a list there produced **duplicated, contradicting copies** (two "shopping list" records with
different items) because the in-place-edit discipline doesn't fit a thing that's constantly rewritten.
That bug is what motivated this separate tier.

The routing the agent applies:
- *something you add to / cross off* → **list** (here)
- *an inert fact recalled when its topic comes up* → **long-term memory** (`app/memory`)
- *a standing behaviour/identity rule, every turn* → **core memory** (`app/prompts`)
- *a future-dated trigger* → **scheduling** (`app/scheduling`, already its own artifact store)

## Shape

- One canonical doc per `(conversation_id, name)` — the name is **case-folded** (`Shopping` ≡
  `shopping`) so a list is never forked by capitalization.
- Items are plain strings, **de-duplicated case-insensitively** on add.
- **Removal** matches the exact item, else a **unique substring** — so a long sentence-item is removed
  by a short distinctive fragment. A fragment hitting several items is **ambiguous** (left intact,
  reported so the model retries); hitting none is **missing** (reported). `RemoveResult` carries
  removed/ambiguous/missing so `list_edit` can give the model clear, actionable feedback.
- This makes lists usable for append-mostly *prose* logs (e.g. contradictions), but it can't edit a
  word *inside* an item — for that, a long-term memory body (`recall_edit`) is the right home.
- `clear=true` soft-deletes the whole list (NisseDbModel `deleted_at`); reads filter `{"deleted_at": None}`.

Mirrors the `app/memory` store/tool pattern; wired in `Conversations._build_list_tools`; index
ensured in `backend.py`.
