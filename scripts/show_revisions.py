"""Print one conversation's change history — who changed what, and the text it replaced.

Companion to `make curate`: the curator run tells you what a pass decided, this tells you what it
actually did to the stores. Reading it is how the owner audits an unattended edit, and how you undo
one — `before` holds the exact text to put back.

    make revisions U=<conversation_id>
    make revisions U=<conversation_id> RUN=<run_id>     # just one night's pass
    uv run --env-file .env python scripts/show_revisions.py <conversation_id> [run_id]
"""

import asyncio
import os
import sys

from pymongo import AsyncMongoClient

_PREVIEW = 400


def _preview(text: str | None) -> str:
    """One field of a revision, trimmed — the full text stays in Mongo."""
    if text is None:
        return "—"
    trimmed = text[:_PREVIEW]
    return f"{trimmed}…" if len(text) > _PREVIEW else trimmed


def _print_revision(doc: dict) -> None:
    """One change: when, by whom, to what, and both sides of it."""
    when = doc["created_at"].strftime("%Y-%m-%d %H:%M")
    run = doc.get("run_id") or "—"
    print(f"\n{when}  {doc['actor']:<9} run={run:<12} {doc['collection']}/{doc['target']}  [{doc['kind']}]")
    print(f"    before: {_preview(doc.get('before'))}")
    print(f"    after:  {_preview(doc.get('after'))}")


async def main() -> None:
    """Print the revisions for one conversation, oldest first, optionally filtered to one run."""
    if len(sys.argv) < 2 or not sys.argv[1]:
        sys.exit("usage: show_revisions.py <conversation_id> [run_id]")
    conversation_id = int(sys.argv[1])
    run_id = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None

    client = AsyncMongoClient(os.environ["MONGODB_URI"], tz_aware=True)
    database = client.get_default_database()
    query: dict = {"conversation_id": conversation_id}
    if run_id:
        query["run_id"] = run_id

    docs = await database["revisions"].find(query, sort=[("created_at", 1)]).to_list(length=None)
    print(f"revisions for conversation {conversation_id}" + (f", run {run_id}" if run_id else "") + f": {len(docs)}")
    for doc in docs:
        _print_revision(doc)
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
