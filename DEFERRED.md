# Deferred work

Planned changes that are intentionally NOT done yet, with enough context to execute later.
Ordered by priority. Remove an item when it ships.

> **Shipped (was items 1 & 5):** the history-layer rewrite landed —
> `baski.agents.MessageHistory` is now a `Protocol`, `InMemoryMessageHistory` is the volatile impl,
> and nisse's `MongoMessageHistory` is a standalone implementation with write-on-commit
> (fire-and-forget) + `flush()` awaited after the answer is sent, async `delete_turns`, and pure
> tool turns written soft-deleted. The prune-awareness prompt lives in `RememberTool` guidance
> (long-term tier) — branches `feat/message-history-protocol` (baski) / `fix/durable-history-trimming` (nisse).

---

## 1. `schema_version` on stored docs — only when a 2nd format actually appears

Defer until a real format change lands (sessions / compaction). No provider switch is planned and
memory is owner-scoped-enough (chat.id == owner id in a private chat), so the migration drivers are
gone. The collection is a single owner's — a later backfill (`add field` + one `update_many`) is
cheap. Add `schema_version: int = 1` to `NisseDbModel` at that point, not now.

---

## 2. Atomic turn-id allocation — only before a 2nd concurrent writer exists

Turn ids are minted in memory (`__enter__` increments `_next_turn_id`), and `_write_turn` uses
`upsert=True`, so two concurrent writers for one conversation could **silently clobber** a turn (no
crash). Safe today: `max_instances=1` (commented as a correctness invariant in
`infrastructure/services/cloud_run_backend.py`) and all entry points share one cached `Conversation`
+ its lock (`flush()` runs under that same lock). Trigger to fix: raising `max_instances`, OR shipping
the **nightly curator as a separate writer**. Fix = allocate turn_id via a Mongo
`find_one_and_update`/`$inc` counter (same CAS pattern as `scheduling/store.py` `claim()`), or switch
`_write_turn` to `insert_one` so a collision fails loud instead of clobbering.

---

## 3. Real compaction (summarize, don't just drop) — quality, later

The transcript is bounded by **dropping** oldest turns (`truncate`) when over budget. That stops the
cost/wedge problem but loses old context. Later, replace drop-oldest with summarize-then-soft-delete:
fold old turns into a summary turn, keep a bounded recent slice. Prior art in `IDEAS.md` ("Flush
before compaction", "Compaction in-place", "iterative-update-the-prior-summary"). Not required for
correctness — only for long-conversation quality.

---

## 4. Sessions / `/new` — additive, no migration; do nothing structural now

Adding sessions later is a nullable `session_id` filter + reset-policy code + cache invalidation on
`/new` (the `Conversations` cache never evicts). `turn_id` is per-conversation monotonic (must NOT
become per-session, or the `(conversation_id, turn_id)` unique index collides). Backfill is trivial
on a single-owner collection. No hedge worth making now.
