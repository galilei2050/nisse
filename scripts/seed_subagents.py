"""Seed a conversation's sub-agents from app/subagents/agents.yml.

Each YAML entry becomes one delegating tool on that conversation's main agent. Upserts on
(conversation_id, name), so re-running updates in place. The definitions live in the YAML; this
only binds them to a conversation id. Pass `all` to re-seed every conversation that already has
configs — the rollout for an agents.yml change (e.g. a new required field like max_turns), so no
live conversation is left on a stale/invalid config.

Run as a module (repo root on sys.path, so `app` imports without a path hack):

    make seed U=<conversation_id>        # one conversation
    make seed U=all                      # re-seed every existing conversation
    uv run --env-file .env python -m scripts.seed_subagents <conversation_id|all>
"""

import asyncio
import os
import sys
from pathlib import Path

import yaml
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.shared.revisions import Actor, acting_as
from app.subagents import SubagentConfig, SubagentStore

_AGENTS_YML = Path(__file__).resolve().parent.parent / "app" / "subagents" / "agents.yml"


async def _seed_one(database: AsyncDatabase, conversation_id: int, definitions: list[dict]) -> None:
    """Upsert every agent defined in agents.yml for one conversation.

    Attributed to the seed script: `save()` records a revision, and without this the change history
    would name the assistant for a roster rewrite that came from a file and a human running `make`.

    A worker the curator retired is left retired. `save` replaces the document whole and does not
    filter on `deleted_at`, so seeding it would revive it — undoing a decision made on the owner's
    evidence, silently, on a run whose purpose was to roll out an unrelated YAML change.
    """
    store = SubagentStore(database, conversation_id=conversation_id)
    retired = await store.retired_names()
    with acting_as(Actor.SEED, run_id=_AGENTS_YML.name):
        for definition in definitions:
            if definition["name"] in retired:
                print(f"skipped '{definition['name']}' for conversation {conversation_id} — retired; re-add by hand")
                continue
            saved = await store.save(SubagentConfig(conversation_id=conversation_id, **definition))
            print(f"seeded '{saved.name}' for conversation {conversation_id} (tools: {', '.join(saved.tool_names)})")


async def main(target: str) -> None:
    """Seed one conversation id, or `all` to re-seed every conversation that already has configs."""
    definitions = yaml.safe_load(_AGENTS_YML.read_text())
    database = AsyncMongoClient(os.environ["MONGODB_URI"], tz_aware=True).get_default_database()
    await SubagentStore.ensure_indexes(database)
    if target == "all":
        conversation_ids = await SubagentStore.all_conversation_ids(database)
        print(f"re-seeding {len(conversation_ids)} conversations: {conversation_ids}")
    else:
        conversation_ids = [int(target)]
    for conversation_id in conversation_ids:
        await _seed_one(database, conversation_id, definitions)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m scripts.seed_subagents <conversation_id|all>")
    asyncio.run(main(sys.argv[1]))
