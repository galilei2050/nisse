"""Assistant — composition root that turns a user message into an agent reply."""

from baski.agents import Agent, AgentConfig, AgentExecuteResult, Listener, ToolSet, noop
from baski.agents.tools import DeleteMessagesTool, ShortTermMemory

from app.assistant.history import MongoMessageHistory
from app.assistant.toolset import build_tools
from app.memory import ForgetTool, MemoryStore, RecallMemoryTool, RememberTool
from app.shared import CoreDeps

NISSE_SYSTEM_PROMPT = (
    "You are Nisse, a personal AI assistant for a single owner. Be concise, direct, and "
    "helpful. When a question needs current or external information, use your tools to look "
    "it up, then answer in plain language."
)

_NO_ANSWER = "I couldn't produce a response — please try rephrasing."


class Assistant:
    """Replies to a message within a persistent, per-conversation chat history."""

    def __init__(
        self,
        *,
        deps: CoreDeps,
        system_prompt: str = NISSE_SYSTEM_PROMPT,
        await_trace: bool = False,
        local_traces_dir: str | None = None,
    ) -> None:
        """Build the domain tools from shared deps; hold the system prompt reused for every reply.

        `await_trace` / `local_traces_dir` are testing knobs (see `app/probe.py`): block on trace
        persistence and write the full trace to a local dir instead of GCS. Off in production.
        """
        self._deps = deps
        self._tools = build_tools(deps)
        self._system_prompt = system_prompt
        self._await_trace = await_trace
        self._local_traces_dir = local_traces_dir

    async def setup(self) -> None:
        """One-time startup: ensure the memory store's indexes exist."""
        await MemoryStore(self._deps.database).ensure_indexes()

    def _build_agent(self, history: MongoMessageHistory, store: MemoryStore, on_event: Listener) -> Agent:
        """Assemble the agent for one reply over the given conversation history and memory."""
        toolset = ToolSet(logger=self._deps.logger)
        for tool in self._tools:
            toolset.add(tool)
        toolset.add(ShortTermMemory())
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
        return Agent(config=config, on_event=on_event)

    async def run(self, *, conversation_id: int, text: str, on_event: Listener = noop) -> AgentExecuteResult:
        """Append the message, run the agent over the conversation, persist history; return the raw result.

        `reply()` wraps this into a user-facing string. Probe/tests call `run()` directly to read
        the result's `trace_id` (to inspect the persisted trace) and token counts.
        """
        history = MongoMessageHistory(
            logger=self._deps.logger, database=self._deps.database, conversation_id=conversation_id
        )
        await history.load()
        with history:
            history.add_user_text(text)

        store = MemoryStore(self._deps.database)
        agent = self._build_agent(history, store, on_event)
        result = await agent.execute()
        await history.save()
        return result

    async def reply(self, *, conversation_id: int, text: str, on_event: Listener = noop) -> str:
        """Reply to a message within the persistent conversation; the chat router's entry point.

        `on_event` receives step events as the agent works — the chat router passes a
        `TelegramProgress` listener so the user sees live progress.
        """
        result = await self.run(conversation_id=conversation_id, text=text, on_event=on_event)

        if not result.response:
            self._deps.logger.warning(
                "Agent produced no user-facing text; sending fallback",
                labels={
                    "traceId": result.trace_id,
                    "turnCount": result.turn_count,
                    "toolCallCount": result.tool_call_count,
                    "outputTokens": result.total_output_tokens,
                },
            )
            return _NO_ANSWER
        return result.response
