"""Print the long-term `memories` collection — live and soft-deleted.

Companion to `app/probe.py`: probe shows what the agent saw/did; this shows the durable result
in Mongo. Memories are scoped per `conversation_id`, so a flat dump of every conversation
conflates the real chat with throwaway `make probe U=…` runs. Therefore:

    make memories U=<conversation_id>   # one conversation, full detail (titles + bodies)
    make memories                       # ALL conversations, grouped by id (titles only)

    uv run --env-file .env python scripts/show_memories.py [conversation_id]
"""

import asyncio
import os
import sys

from pymongo import AsyncMongoClient


def _line(m: dict) -> str:
    """One-line label for a memory: state, id, category, source, title."""
    state = "DELETED" if m.get("deleted_at") else "live"
    src = m.get("source", {})
    ref = f":{src['ref']}" if src.get("ref") else ""
    return f"  {state:8} [{m.get('public_id')}] {m.get('category')} · {src.get('kind')}{ref} — {m.get('title')!r}"


async def main(conversation_id: int | None) -> None:
    db = AsyncMongoClient(os.environ["MONGODB_URI"], tz_aware=True).get_default_database()
    flt = {"conversation_id": conversation_id} if conversation_id is not None else {}
    rows = await db["memories"].find(flt).sort([("conversation_id", 1), ("created_at", 1)]).to_list(None)

    if conversation_id is not None:
        # One conversation: full detail, bodies included.
        live = sum(1 for m in rows if not m.get("deleted_at"))
        print(f"conversation_id={conversation_id}  total={len(rows)}  live={live}  soft-deleted={len(rows) - live}")
        for m in rows:
            print(f"{_line(m)}\n         body={m.get('body')!r}")
        return

    # All conversations: grouped, titles only — so the real chat is never conflated with probe runs.
    groups: dict[object, list[dict]] = {}
    for m in rows:
        groups.setdefault(m.get("conversation_id"), []).append(m)
    for conv, mems in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        live = sum(1 for m in mems if not m.get("deleted_at"))
        print(f"\n=== conversation_id={conv} — {len(mems)} total, {live} live ===")
        for m in mems:
            print(_line(m))
    print(f"\n{len(rows)} total across {len(groups)} conversations")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else None))
