"""Assistant — composition root that turns a user message into an agent reply."""

from baski.agents import Agent, AgentConfig, Listener, ToolSet, noop
from baski.agents.tools import DeleteMessagesTool, ShortTermMemory

from app.assistant.history import MongoMessageHistory
from app.assistant.toolset import build_tools
from app.shared import CoreDeps

NISSE_SYSTEM_PROMPT = (
    "You are Nisse, a personal AI assistant for a single owner. Be concise, direct, and "
    "helpful. When a question needs current or external information, use your tools to look "
    "it up, then answer in plain language."
)

_NO_ANSWER = "I couldn't produce a response — please try rephrasing."


class Assistant:
    """Replies to a message within a persistent, per-conversation chat history."""

    def __init__(self, *, deps: CoreDeps, system_prompt: str = NISSE_SYSTEM_PROMPT) -> None:
        """Build the domain tools from shared deps; hold the system prompt reused for every reply."""
        self._deps = deps
        self._tools = build_tools(deps)
        self._system_prompt = system_prompt

    def _build_agent(self, history: MongoMessageHistory, on_event: Listener) -> Agent:
        """Assemble the agent for one reply over the given conversation history."""
        short_term_memory = ShortTermMemory()

        toolset = ToolSet(logger=self._deps.logger)
        for tool in self._tools:
            toolset.add(tool)
        toolset.add(short_term_memory)
        toolset.add(DeleteMessagesTool(history))

        config = AgentConfig(
            logger=self._deps.logger,
            toolset=toolset,
            message_history=history,
            short_term_memory=short_term_memory,
            anthropic_client=self._deps.anthropic,
            database=self._deps.database,
            bucket_name=self._deps.bucket_name,
            system_prompt=self._system_prompt,
        )
        return Agent(config=config, on_event=on_event)

    async def reply(self, *, conversation_id: int, text: str, on_event: Listener = noop) -> str:
        """Append the message to the conversation, run the agent, and persist the new history.

        `on_event` receives step events as the agent works — the chat router passes a
        `TelegramProgress` listener so the user sees live progress.
        """
        history = MongoMessageHistory(
            logger=self._deps.logger, database=self._deps.database, conversation_id=conversation_id
        )
        await history.load()
        with history:
            history.add_user_text(text)

        agent = self._build_agent(history, on_event)
        result = await agent.execute()
        await history.save()

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
