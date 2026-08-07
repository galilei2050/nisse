"""Conversations — registry that builds each chat's agent once and reuses it."""

from baski.agents import Agent, AgentConfig, ToolSet
from baski.agents.tools import DeleteMessagesTool, ShortTermMemory

from app.assistant.conversation import Conversation
from app.assistant.history import MongoMessageHistory
from app.assistant.judge import CuratedJudge
from app.prompts import PromptStore
from app.shared import CoreDeps
from app.subagents import SubagentStore, SubagentTool

MAIN_MODEL = "claude-opus-5"  # the main agent's model (sub-agents pick their own in agents.yml)

# The main Assistant's tool spec — the names it builds from the shared registry (`deps.tools`). The
# main agent gets only the GENERAL web tools; the specialized SerpApi leaves (maps/news/events/jobs,
# amazon/youtube) stay registered for sub-agents (e.g. retrieval) but off the always-on roster to keep
# its per-turn schema lean. The researcher-only `hypothesis_tree` is deliberately absent.
MAIN_TOOLS: list[str] = [
    "google_search",
    "google_ai_answer",
    "browse_website",
    "memory",
    "lists",
    "scheduling",
    "core_memory",
    "ask_user",  # mid-turn clarifying question with tappable options (needs a transport; probe fakes one)
]


class Conversations:
    """Builds and caches one `Conversation` per conversation_id; reused for every later reply.

    Lifecycle: long-lived — one registry for the bot (holds the per-conversation cache).
    """

    def __init__(self, *, deps: CoreDeps, system_prompt: str) -> None:
        """Hold the shared deps (which carry the tool registry + trace-sink settings) and the prompt."""
        self._deps = deps
        self._system_prompt = system_prompt
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
        """Assemble one chat's agent: the main tool spec from the registry + the loop-bound primitives.

        The registry builds every capability tool (search, memory, lists, scheduling, core memory) —
        the same mechanism sub-agents use. Only the two loop-bound primitives are wired by hand: the
        short-term scratchpad (its instance is handed to `Conversation` to clear per reply) and
        `DeleteMessagesTool` (needs this agent's live history).
        """
        history = MongoMessageHistory(database=self._deps.database, conversation_id=conversation_id)
        await history.load()
        short_term = ShortTermMemory()

        toolset = ToolSet()
        toolset.add(short_term)
        toolset.add(DeleteMessagesTool(history))
        for tool in self._deps.tools.build(MAIN_TOOLS, self._deps, conversation_id):
            toolset.add(tool)
        for tool in await self._build_subagent_tools(conversation_id):
            toolset.add(tool)

        config = AgentConfig(
            toolset=toolset,
            model=MAIN_MODEL,
            message_history=history,
            anthropic_client=self._deps.anthropic,
            database=self._deps.database,
            bucket_name=self._deps.bucket_name,
            system_prompt=self._system_prompt,
            await_trace=self._deps.await_trace,
            local_traces_dir=self._deps.local_traces_dir,
            # Per conversation, not process-wide: half its rubric is this chat's own `judge_rules`
            # document, which the nightly curator maintains.
            judge=CuratedJudge(
                PromptStore(self._deps.database, conversation_id=conversation_id), project=self._deps.judge_project
            ),
        )
        return Conversation(agent=Agent(config=config), history=history, short_term=short_term)

    async def _build_subagent_tools(self, conversation_id: int) -> list[SubagentTool]:
        """Configured sub-agents (seeded in Mongo per chat), each exposed as one delegating tool.

        Every config is passed as a sibling to every top-level tool, so an orchestrator sub-agent can
        resolve a sibling name in its `tool_names` into a child (delegation is allowed when there are
        siblings — a worker whose `tool_names` are all registry tools simply never delegates). Children
        are built with no siblings, capping nesting at one level. Each sub-agent builds its tools
        through the same registry (`deps.tools`).
        """
        store = SubagentStore(self._deps.database, conversation_id=conversation_id)
        configs = await store.list()
        siblings = {config.name: config for config in configs}
        return [
            SubagentTool(config, self._deps, conversation_id=conversation_id, siblings=siblings) for config in configs
        ]
