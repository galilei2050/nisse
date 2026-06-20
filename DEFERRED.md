# Deferred work

Concrete follow-ups, each gated by a trigger. Remove an item when it ships.

## Atomic turn-id allocation — before a 2nd concurrent writer exists

Turn ids are minted in memory and `_write_turn` upserts, so two concurrent writers for one
conversation would silently clobber a turn. Safe today: `max_instances=1` (commented as a
correctness invariant in `infrastructure/services/cloud_run_backend.py`) and every entry point
shares one cached `Conversation` + its lock. **Trigger:** raising `max_instances`, or shipping the
nightly curator as a separate writer. **Fix:** allocate turn_id via a Mongo
`find_one_and_update`/`$inc` counter (like `scheduling/store.py` `claim()`), or `insert_one` so a
collision fails loud instead of clobbering.

## Not planned

- **Compaction / summarization** — deliberate: drop-oldest trimming is the chosen policy.
