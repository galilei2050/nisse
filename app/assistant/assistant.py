"""Assistant — the thin TG↔agent layer: turns a user message into an agent reply."""

from baski.agents import AgentExecuteResult, Listener, noop

from app.assistant.conversations import Conversations
from app.memory import MemoryStore
from app.prompts import PromptStore
from app.shared import CoreDeps

NISSE_SYSTEM_PROMPT = (
    "You are Nisse, a personal AI assistant for a single owner. Be concise, direct, and "
    "helpful. When a question needs current or external information, use your tools to look "
    "it up, then answer in plain language."
)

_NO_ANSWER = "I couldn't produce a response — please try rephrasing."


def _humanize_tokens(n: int) -> str:
    """Compact token count for the reply footer: 12_400 → '12.4k', 64_000 → '64k'."""
    return f"{n / 1000:.1f}k".replace(".0k", "k")


def _footer(result: AgentExecuteResult) -> str:
    """One-line cost + current context-size note appended to every answer."""
    return f"\n\n— ${result.total_cost:.4f} · контекст {_humanize_tokens(result.context_tokens)}"


class Assistant:
    """Replies to a message by driving the conversation's reused agent (built/cached by `Conversations`).

    Lifecycle: long-lived — one per bot (cached_property in NisseBot), reused for every message.
    """

    def __init__(
        self,
        *,
        deps: CoreDeps,
        system_prompt: str = NISSE_SYSTEM_PROMPT,
        await_trace: bool = False,
        local_traces_dir: str | None = None,
    ) -> None:
        """Build the conversation registry from shared deps.

        `await_trace` / `local_traces_dir` are testing knobs (see `app/probe.py`): block on trace
        persistence and write the full trace to a local dir instead of GCS. Off in production.
        """
        self._deps = deps
        self._conversations = Conversations(
            deps=deps,
            system_prompt=system_prompt,
            await_trace=await_trace,
            local_traces_dir=local_traces_dir,
        )

    async def setup(self) -> None:
        """One-time startup: ensure the memory and prompt stores' indexes exist."""
        await MemoryStore.ensure_indexes(self._deps.database)
        await PromptStore.ensure_indexes(self._deps.database)

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
        return result.response + _footer(result)

    async def flush(self, *, conversation_id: int) -> None:
        """Await the conversation's durable history writes — called after the answer is sent."""
        conversation = await self._conversations.get(conversation_id)
        await conversation.flush()
