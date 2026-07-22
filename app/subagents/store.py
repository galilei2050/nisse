"""Configurable sub-agents — per-conversation configs in Mongo `subagents`.

Seeded externally (admin script); the bot reads them when a chat's agent is built and exposes
each as one delegating tool. See app/subagents/CLAUDE.md.
"""

from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.shared.models import NisseDbModel
from app.shared.mongo import ensure_index

_COLLECTION = "subagents"


class SubagentConfig(NisseDbModel):
    """One configured sub-agent for a conversation.

    Lifecycle: a data record (one Mongo doc), scoped to `conversation_id`; seeded externally and
    read once when the chat's agent is built. Every field is a required config axis — a config
    that omits one is a seed error, not a runtime default.
    """

    conversation_id: int
    name: str  # agent-facing key, unique per conversation; becomes the exposed tool's name
    description: str  # what the parent reads to decide when to delegate; becomes the tool description
    system_prompt: str  # the child's system prompt (owns the return-compression discipline)
    model: str  # concrete model id, e.g. a cheaper one for the child
    tool_names: list[str]  # registry keys, "hypothesis_tree", and/or sibling sub-agent names (resolved at build)
    context_tokens: int  # the child's ephemeral history budget for one run
    max_turns: int  # hard cap on the child's loop; told its budget each turn, forced to answer on the last
    judge_prompt: str  # the child's completeness rubric (its own GeminiJudge instructions)


class SubagentStore:
    """Read a conversation's sub-agent configs; the write path (`save`) is seed-only.

    Lifecycle: per-conversation — built in `_build_subagent_tools`.
    """

    def __init__(self, database: AsyncDatabase, *, conversation_id: int) -> None:
        """Bind to the subagents collection for one conversation; every query is scoped to it."""
        self._collection = database[_COLLECTION]
        self._conversation_id = conversation_id

    @staticmethod
    async def ensure_indexes(database: AsyncDatabase) -> None:
        """Unique (conversation_id, name) so names never collide in the parent's toolset."""
        await ensure_index(database[_COLLECTION], [("conversation_id", 1), ("name", 1)], unique=True)

    @staticmethod
    async def all_conversation_ids(database: AsyncDatabase) -> list[int]:
        """Every conversation that has live sub-agent configs — for `seed all` (re-seed after a config change)."""
        return sorted(await database[_COLLECTION].distinct("conversation_id", {"deleted_at": None}))

    async def list(self) -> list[SubagentConfig]:
        """Every live sub-agent config in this conversation."""
        query = {"conversation_id": self._conversation_id, "deleted_at": None}
        return [SubagentConfig.model_validate(doc) async for doc in self._collection.find(query)]

    async def save(self, config: SubagentConfig) -> SubagentConfig:
        """Insert or replace by (conversation_id, name) — idempotent, for the seed script."""
        result = await self._collection.find_one_and_replace(
            {"conversation_id": self._conversation_id, "name": config.name},
            config.model_dump(exclude={"id"}),
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return SubagentConfig.model_validate(result)
