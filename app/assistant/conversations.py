"""Conversations — registry that builds each chat's agent once and reuses it."""

from baski.agents import Agent, AgentConfig, ToolSet
from baski.agents.tools import DeleteMessagesTool, ShortTermMemory

from app.assistant.conversation import Conversation
from app.assistant.history import MongoMessageHistory
from app.assistant.toolset import build_tools
from app.memory import ForgetTool, MemoryStore, RecallMemoryTool, RememberTool
from app.shared import CoreDeps


class Conversations:
    """Builds and caches one `Conversation` per conversation_id; reused for every later reply."""

    def __init__(
        self,
        *,
        deps: CoreDeps,
        system_prompt: str,
        await_trace: bool = False,
        local_traces_dir: str | None = None,
    ) -> None:
        """Hold the shared deps + the prebuilt domain tools used to assemble every conversation's agent."""
        self._deps = deps
        self._tools = build_tools(deps)  # stateless domain tools, shared across conversations
        self._system_prompt = system_prompt
        self._await_trace = await_trace
        self._local_traces_dir = local_traces_dir
        self._conversations: dict[int, Conversation] = {}

    async def get(self, conversation_id: int) -> Conversation:
        """The conversation's reused instance, built on first use.

        Single-owner bot: a cold-start burst can't realistically race the first build, so the
        plain get-or-create is enough — no creation lock. Once cached, every reply reuses it.
        """
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            conversation = await self._build(conversation_id)
            self._conversations[conversation_id] = conversation
        return conversation

    async def _build(self, conversation_id: int) -> Conversation:
        """Assemble one chat's agent: history loaded from Mongo, stateful tools scoped to the chat."""
        history = MongoMessageHistory(
            logger=self._deps.logger, database=self._deps.database, conversation_id=conversation_id
        )
        await history.load()
        # Long-term memory persists across replies, so its store MUST be scoped to the chat —
        # never let one conversation's memories leak into another's. See app/CLAUDE.md "Tool =".
        store = MemoryStore(self._deps.database, conversation_id=conversation_id)
        short_term = ShortTermMemory()

        toolset = ToolSet(logger=self._deps.logger)
        for tool in self._tools:
            toolset.add(tool)
        toolset.add(short_term)
        toolset.add(DeleteMessagesTool(history))
        toolset.add(RememberTool(store))
        toolset.add(RecallMemoryTool(store))  # reads the index live from the store each turn
        toolset.add(ForgetTool(store))

        config = AgentConfig(
            logger=self._deps.logger,
            toolset=toolset,
            message_history=history,
            anthropic_client=self._deps.anthropic,
            database=self._deps.database,
            bucket_name=self._deps.bucket_name,
            system_prompt=self._system_prompt,
            await_trace=self._await_trace,
            local_traces_dir=self._local_traces_dir,
        )
        return Conversation(agent=Agent(config=config), history=history, short_term=short_term)
