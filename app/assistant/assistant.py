"""Assistant — the thin TG↔agent layer: turns a user message into an agent reply."""

from baski.agents import AgentExecuteResult, Listener, noop

from app.assistant.conversations import Conversations
from app.memory import MemoryStore
from app.scheduling import Scheduling
from app.shared import CoreDeps

NISSE_SYSTEM_PROMPT = (
    "You are Nisse, a personal AI assistant for a single owner. Be concise, direct, and "
    "helpful. When a question needs current or external information, use your tools to look "
    "it up, then answer in plain language."
)

_NO_ANSWER = "I couldn't produce a response — please try rephrasing."


class Assistant:
    """Replies to a message by driving the conversation's reused agent (built/cached by `Conversations`)."""

    def __init__(  # noqa: PLR0913 — composition root: deps + prompt + 2 trace knobs + scheduling
        self,
        *,
        deps: CoreDeps,
        system_prompt: str = NISSE_SYSTEM_PROMPT,
        await_trace: bool = False,
        local_traces_dir: str | None = None,
        scheduling: Scheduling | None = None,
    ) -> None:
        """Build the conversation registry from shared deps + the prebuilt domain tools.

        `await_trace` / `local_traces_dir` are testing knobs (see `app/probe.py`): block on trace
        persistence and write the full trace to a local dir instead of GCS. Off in production.
        `scheduling` wires the reminder tools; None in polling mode (no public fire callback).
        """
        self._deps = deps
        self._conversations = Conversations(
            deps=deps,
            system_prompt=system_prompt,
            await_trace=await_trace,
            local_traces_dir=local_traces_dir,
            scheduling=scheduling,
        )

    async def setup(self) -> None:
        """One-time startup: ensure the memory store's indexes exist."""
        await MemoryStore.ensure_indexes(self._deps.database)

    async def run(self, *, conversation_id: int, text: str, on_event: Listener = noop) -> AgentExecuteResult:
        """Drive the conversation's reused agent over the new message; return the raw result.

        `reply()` wraps this into a user-facing string. Probe/tests call `run()` directly to read
        the result's `trace_id` (to inspect the persisted trace) and token counts.
        """
        conversation = await self._conversations.get(conversation_id)
        return await conversation.reply(text=text, on_event=on_event)

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
