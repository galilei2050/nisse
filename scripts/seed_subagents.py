"""Seed a conversation's sub-agents from app/subagents/agents.yml.

Each YAML entry becomes one delegating tool on that conversation's main agent. Upserts on
(conversation_id, name), so re-running updates in place. The definitions live in the YAML; this
only binds them to a conversation id.

Run as a module (repo root on sys.path, so `app` imports without a path hack):

    make seed U=<conversation_id>
    uv run --env-file .env python -m scripts.seed_subagents <conversation_id>
"""

import asyncio
import os
import sys
from pathlib import Path

import yaml
from pymongo import AsyncMongoClient

from app.subagents import SubagentConfig, SubagentStore

_AGENTS_YML = Path(__file__).resolve().parent.parent / "app" / "subagents" / "agents.yml"


async def main(conversation_id: int) -> None:
    """Upsert every agent defined in agents.yml for one conversation."""
    definitions = yaml.safe_load(_AGENTS_YML.read_text())
    database = AsyncMongoClient(os.environ["MONGODB_URI"], tz_aware=True).get_default_database()
    await SubagentStore.ensure_indexes(database)
    store = SubagentStore(database, conversation_id=conversation_id)
    for definition in definitions:
        saved = await store.save(SubagentConfig(conversation_id=conversation_id, **definition))
        print(f"seeded '{saved.name}' for conversation {conversation_id} (tools: {', '.join(saved.tool_names)})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m scripts.seed_subagents <conversation_id>")
    asyncio.run(main(int(sys.argv[1])))
