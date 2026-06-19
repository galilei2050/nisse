"""Print the long-term `memories` collection — live and soft-deleted — for manual probe checks.

Companion to `app/probe.py`: probe shows what the agent saw/did; this shows the durable
result in Mongo. Run with the same env as the probe:

    uv run --env-file .env python scripts/show_memories.py
"""

import asyncio
import os

from pymongo import AsyncMongoClient


async def main() -> None:
    db = AsyncMongoClient(os.environ["MONGODB_URI"], tz_aware=True).get_default_database()
    cur = db["memories"].find().sort("created_at", 1)
    rows = [doc async for doc in cur]
    for m in rows:
        state = "DELETED" if m.get("deleted_at") else "live"
        src = m.get("source", {})
        print(
            f"{state:8} [{m.get('public_id')}] {m.get('category')} · {src.get('kind')}"
            f"{':' + src['ref'] if src.get('ref') else ''} — {m.get('title')!r}\n"
            f"         body={m.get('body')!r}"
        )
    live = sum(1 for m in rows if not m.get("deleted_at"))
    print(f"\n{len(rows)} total, {live} live, {len(rows) - live} soft-deleted")


if __name__ == "__main__":
    asyncio.run(main())
