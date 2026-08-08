"""Append the citation rule to every live `retrieval` prompt, keeping what the curator wrote.

Not a re-seed: the live documents have diverged from `agents.yml` and re-seeding would discard the
curator's work (app/subagents/CLAUDE.md). This appends to whatever text is there, goes through
`SubagentStore.save` so the previous version survives as a revision, and skips a config that already
carries the rule.

    uv run python -m scripts.patch_retrieval_prompt          # dry run: prints what would change
    uv run python -m scripts.patch_retrieval_prompt --apply
"""

import argparse
import asyncio
import os
import pathlib

from pymongo import AsyncMongoClient

from app.subagents.store import SubagentStore

MARKER = "CITATION RULE"
RULE = """

CITATION RULE — this one is not negotiable.
Cite ONLY pages you actually opened with `browse_website`. A url you did not open is not a source. When a claim rests on a search result you did not open, say so in the line itself: `(search result, not opened)`. When a page refuses to load, write that plainly and leave the number out — never substitute an estimate, and never present a figure you did not read as if you read it. An invented number under a real-looking link is the one failure your caller cannot detect."""


def _uri() -> str:
    """Read nisse's own .env — an inherited MONGODB_URI in the shell points at another project."""
    for line in pathlib.Path(".env").read_text().splitlines():
        if line.startswith("MONGODB_URI="):
            return line.split("=", 1)[1].strip()
    return os.environ["MONGODB_URI"]


async def main(apply: bool) -> None:
    """Patch every conversation's `retrieval` worker, or show what patching would do."""
    database = AsyncMongoClient(_uri(), tz_aware=True).get_default_database()
    print(f"db: {database.name}   mode: {'APPLY' if apply else 'dry run'}\n")

    ids = await SubagentStore.all_conversation_ids(database)
    for conversation_id in ids:
        store = SubagentStore(database, conversation_id=conversation_id)
        config = await store.get("retrieval")
        if config is None:
            print(f"{conversation_id}: no `retrieval` worker")
            continue
        if MARKER in config.system_prompt:
            print(f"{conversation_id}: already carries the rule ({len(config.system_prompt)} chars)")
            continue
        patched = config.model_copy(update={"system_prompt": config.system_prompt + RULE})
        print(f"{conversation_id}: {len(config.system_prompt)} -> {len(patched.system_prompt)} chars")
        if apply:
            await store.save(patched)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Append the citation rule to live retrieval prompts.")
    parser.add_argument("--apply", action="store_true", help="write; without it, only print the plan")
    asyncio.run(main(parser.parse_args().apply))
