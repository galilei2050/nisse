"""Print the `conversation_turns` collection for one conversation — active and soft-deleted.

Companion to `app/probe.py` and `scripts/show_memories.py`: probe shows what the agent did;
this shows the durable transcript in Mongo so you can verify pruning (which turns are active
vs soft-deleted), recoverability (deleted turns still present), and turn-id integrity.

    make turns U=<conversation_id>
    uv run --env-file .env python scripts/show_turns.py <conversation_id>
"""

import asyncio
import os
import sys

from pymongo import AsyncMongoClient


def _block_label(block: dict) -> str:
    """One-line label for a content block: text preview, tool name, or block type."""
    btype = block.get("type")
    if btype == "text":
        return f"text: {block.get('text', '')[:90]!r}"
    if btype == "tool_use":
        return f"tool_use: {block.get('name')}"
    if btype == "tool_result":
        return f"tool_result: id={str(block.get('tool_use_id', ''))[:12]}"
    return f"block: {btype}"


def _print_turn(doc: dict) -> None:
    """Print one turn document: id, state, and each message's blocks."""
    state = "DELETED" if doc.get("deleted_at") else "active"
    messages = doc.get("messages", [])
    print(f"\n  turn_id={doc['turn_id']} [{state}]  messages={len(messages)}")
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            print(f"    [{msg.get('role')}] text: {content[:90]!r}")
            continue
        for block in content:
            print(f"    [{msg.get('role')}] {_block_label(block)}")


async def main(conversation_id: int) -> None:
    db = AsyncMongoClient(os.environ["MONGODB_URI"], tz_aware=True).get_default_database()
    docs = await db["conversation_turns"].find({"conversation_id": conversation_id}).sort("turn_id", 1).to_list(None)

    active = sum(1 for d in docs if not d.get("deleted_at"))
    print(f"conversation_id={conversation_id}  total={len(docs)}  active={active}  soft-deleted={len(docs) - active}")
    for doc in docs:
        _print_turn(doc)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1])))
