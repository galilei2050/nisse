"""Print one conversation's change history — who changed what, and the text it replaced.

Companion to `make curate`: the curator run tells you what a pass decided, this tells you what it
actually did to the stores. Reading it is how the owner audits an unattended edit, and how you undo
one — `before` holds the exact text to put back.

    make revisions U=<conversation_id>
    make revisions U=<conversation_id> RUN=<run_id>     # just one night's pass
    make revisions U=<conversation_id> REV=<revision_id>  # one change, both sides in full
    uv run --env-file .env python scripts/show_revisions.py <conversation_id> [run_id] [revision_id]

`REV` is what makes an undo possible: the listing trims to a preview, and a core-memory block or a
sub-agent config is far longer than that, so the text you would put back is exactly the part the
listing cuts off.
"""

import asyncio
import os
import sys

from bson import ObjectId
from pymongo import AsyncMongoClient

_PREVIEW = 400


def _preview(text: str | None) -> str:
    """One field of a revision, trimmed — the full text stays in Mongo."""
    if text is None:
        return "—"
    trimmed = text[:_PREVIEW]
    return f"{trimmed}…" if len(text) > _PREVIEW else trimmed


def _print_revision(doc: dict) -> None:
    """One change: when, by whom, to what, and both sides of it — trimmed, with the id to expand it."""
    when = doc["created_at"].strftime("%Y-%m-%d %H:%M")
    run = doc.get("run_id") or "—"
    print(f"\n{when}  {doc['actor']:<9} run={run:<12} {doc['collection']}/{doc['target']}  [{doc['kind']}]")
    print(f"    id:     {doc['_id']}")
    print(f"    before: {_preview(doc.get('before'))}")
    print(f"    after:  {_preview(doc.get('after'))}")


def _print_in_full(doc: dict) -> None:
    """One change with both sides untrimmed — the text an undo is copied from."""
    when = doc["created_at"].strftime("%Y-%m-%d %H:%M")
    print(f"{when}  {doc['actor']}  {doc['collection']}/{doc['target']}  [{doc['kind']}]  run={doc.get('run_id') or '—'}")
    for side in ("before", "after"):
        print(f"\n=== {side} ===")
        print(doc.get(side) if doc.get(side) is not None else "—")


async def main() -> None:
    """Print the revisions for one conversation, oldest first, optionally filtered to one run."""
    if len(sys.argv) < 2 or not sys.argv[1]:
        sys.exit("usage: show_revisions.py <conversation_id> [run_id] [revision_id]")
    conversation_id = int(sys.argv[1])
    run_id = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
    revision_id = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None

    client = AsyncMongoClient(os.environ["MONGODB_URI"], tz_aware=True)
    database = client.get_default_database()

    if revision_id:
        doc = await database["revisions"].find_one({"conversation_id": conversation_id, "_id": ObjectId(revision_id)})
        if doc is None:
            sys.exit(f"no revision {revision_id} in conversation {conversation_id}")
        _print_in_full(doc)
        await client.close()
        return

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
