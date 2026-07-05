"""Conversations — registry that builds each chat's agent once and reuses it."""

from baski.agents import Agent, AgentConfig, ToolSet
from baski.agents.tool import Tool
from baski.agents.tools import DeleteMessagesTool, ShortTermMemory

from app.assistant.conversation import Conversation
from app.assistant.history import MongoMessageHistory
from app.lists import ListEditTool, ListShowTool, ListStore
from app.memory import EditMemoryTool, ForgetTool, MemoryStore, RecallMemoryTool, RememberTool
from app.prompts import CoreMemoryTool, PromptStore
from app.scheduling import CancelScheduleTool, RemindTool, RoutineTool, ScheduleStore, SchedulingService
from app.shared import CoreDeps
from app.subagents import TOOL_REGISTRY, SubagentStore, SubagentTool, build_tools


class Conversations:
    """Builds and caches one `Conversation` per conversation_id; reused for every later reply.

    Lifecycle: long-lived — one registry for the bot (holds the per-conversation cache).
    """

    def __init__(
        self,
        *,
        deps: CoreDeps,
        system_prompt: str,
        await_trace: bool = False,
        local_traces_dir: str | None = None,
    ) -> None:
        """Hold the shared deps + reply settings used to assemble every conversation's agent."""
        self._deps = deps
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
        """Assemble one chat's agent inline from CoreDeps. Add a tool domain → a new `_build_*_tools`."""
        history = MongoMessageHistory(database=self._deps.database, conversation_id=conversation_id)
        await history.load()
        short_term = ShortTermMemory()

        toolset = ToolSet()
        toolset.add(short_term)
        toolset.add(DeleteMessagesTool(history))
        for tool in [
            *self._build_web_tools(),
            *self._build_memory_tools(conversation_id),
            *self._build_list_tools(conversation_id),
            *self._build_scheduling_tools(conversation_id),
            *await self._build_subagent_tools(conversation_id),
            CoreMemoryTool(PromptStore(self._deps.database, conversation_id=conversation_id)),
        ]:
            toolset.add(tool)

        config = AgentConfig(
            toolset=toolset,
            message_history=history,
            anthropic_client=self._deps.anthropic,
            database=self._deps.database,
            bucket_name=self._deps.bucket_name,
            system_prompt=self._system_prompt,
            await_trace=self._await_trace,
            local_traces_dir=self._local_traces_dir,
            judge=self._deps.judge,
        )
        return Conversation(agent=Agent(config=config), history=history, short_term=short_term)

    def _build_web_tools(self) -> list[Tool]:
        """Search + browsing leaves, built from the shared tool registry (also the sub-agent whitelist)."""
        return build_tools(TOOL_REGISTRY.keys(), self._deps)

    def _build_memory_tools(self, conversation_id: int) -> list[Tool]:
        """Long-term memory — store scoped to the chat so memories never cross conversations."""
        store = MemoryStore(self._deps.database, conversation_id=conversation_id)
        return [RememberTool(store), RecallMemoryTool(store), EditMemoryTool(store), ForgetTool(store)]

    def _build_list_tools(self, conversation_id: int) -> list[Tool]:
        """Named lists (ARTIFACT tier) — store scoped to the chat so lists never cross conversations."""
        store = ListStore(self._deps.database, conversation_id=conversation_id)
        return [ListEditTool(store), ListShowTool(store)]

    def _build_scheduling_tools(self, conversation_id: int) -> list[Tool]:
        """Reminders/routines — built in every mode.

        The scheduler is always present (a LoggingScheduler in polling/probe), so these tools always
        exist; only in webhook mode does a fire actually call back and run.
        """
        service = SchedulingService(scheduler=self._deps.scheduler, endpoint=self._deps.schedule_endpoint)
        store = ScheduleStore(self._deps.database, conversation_id=conversation_id)
        return [RemindTool(store, service), RoutineTool(store, service), CancelScheduleTool(store)]

    async def _build_subagent_tools(self, conversation_id: int) -> list[Tool]:
        """Configured sub-agents (seeded in Mongo per chat), each exposed as one delegating tool.

        Every config is passed as a sibling to every top-level tool so an orchestrator sub-agent can
        resolve a sibling name in its `tool_names` into a child. Top-level tools get `can_delegate=True`
        (a worker whose `tool_names` are all leaves simply never uses it); children are built as leaves,
        capping nesting at one level.
        """
        store = SubagentStore(self._deps.database, conversation_id=conversation_id)
        configs = await store.list()
        siblings = {config.name: config for config in configs}
        return [SubagentTool(config, self._deps, siblings=siblings, can_delegate=True) for config in configs]
