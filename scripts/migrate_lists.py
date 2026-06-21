"""One-off migration: move shopping-list memories out of long-term `memories` into the `lists` tier.

Lists were mis-stored in long-term memory (the bug the ARTIFACT tier fixes), where they duplicated and
contradicted. This finds live memories that are really lists (title matches a shopping/list pattern),
parses their bullet items into a `lists` doc per conversation, and soft-deletes the source memory.

    uv run --env-file .env python scripts/migrate_lists.py          # dry-run: print the plan
    uv run --env-file .env python scripts/migrate_lists.py --apply  # perform it

Idempotent: after --apply the source memories are soft-deleted, so a re-run finds nothing.
Reversible: the memory is soft-deleted (deleted_at), not destroyed — recoverable if needed.
"""

import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymongo import AsyncMongoClient  # noqa: E402

from app.lists.store import ListStore  # noqa: E402
from app.memory.store import MemoryStore  # noqa: E402

_LIST_TITLE = re.compile(r"список|shopping|grocery|противоречи|contradiction", re.IGNORECASE)
_ITEM_LINE = re.compile(r"^(?:[-•*]|\d+[.)])\s*(.+)")  # a bullet ('- x') or a numbered ('1. x') item


def _list_name(title: str) -> str:
    """Canonical lower-case list name derived from the memory title."""
    low = title.lower()
    if "покуп" in low or "shopping" in low or "grocery" in low:
        return "покупки"
    if "противоречи" in low or "contradiction" in low:
        return "противоречия"
    return title.strip().lower()


def _parse_items(body: str) -> list[str]:
    """Items from a memory body — bullet/numbered lines if any, else a 'header: a, b, c' line."""
    listed = [m.group(1).strip() for line in body.splitlines() if (m := _ITEM_LINE.match(line.strip()))]
    if listed:
        return listed
    # No bullets/numbers: take the longest 'header: a, b, c' line, split the part after the colon on commas.
    inline = [ln for ln in body.splitlines() if ":" in ln and "," in ln.split(":", 1)[1]]
    if not inline:
        return []
    tail = max(inline, key=len).split(":", 1)[1]
    return [part.strip().rstrip(".") for part in tail.split(",") if part.strip()]


async def main(apply: bool) -> None:
    db = AsyncMongoClient(os.environ["MONGODB_URI"], tz_aware=True).get_default_database()
    candidates = [
        m
        async for m in db["memories"].find({"deleted_at": None})
        if _LIST_TITLE.search(m.get("title", "")) or _LIST_TITLE.search(m.get("body", ""))
    ]
    if not candidates:
        print("Nothing to migrate (no live list-shaped memories).")
        return

    for m in candidates:
        conv, pid, title = m["conversation_id"], m["public_id"], m.get("title", "")
        name = _list_name(title)
        items = _parse_items(m.get("body", ""))
        print(f"\nconversation_id={conv}  [{pid}] {title!r}")
        if not items:
            print("  ! no items parsed — SKIPPED (memory left intact, nothing destroyed)")
            continue
        print(f"  → list '{name}' += {items}")
        if not apply:
            continue
        await ListStore(db, conversation_id=conv).add(name, items)
        await MemoryStore(db, conversation_id=conv).soft_delete(pid)
        print("  ✓ migrated and memory soft-deleted")

    print(f"\n{'APPLIED' if apply else 'DRY-RUN'} — {len(candidates)} memory record(s).")
    if not apply:
        print("Re-run with --apply to perform it.")


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
