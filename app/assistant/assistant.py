"""Assistant — the thin TG↔agent layer: turns a user message into an agent reply."""

import logging

from baski.agents import AgentExecuteResult, Listener, noop
from baski.server.logger import log_context

from app.assistant.conversations import Conversations
from app.memory import MemoryStore
from app.prompts import PromptStore
from app.shared import CoreDeps
from app.shared.blocks import Media

logger = logging.getLogger(__name__)

NISSE_SYSTEM_PROMPT = (
    "You are Nisse, a personal AI assistant for a single owner. Be concise and direct. When a question "
    "needs current or external information, use your tools to look it up, then answer in plain language.\n"
    "Act, don't ask: never ask permission to do something you can just do — phrasings like \"want me "
    'to add…/search…/dig deeper?" (in any language) are forbidden; do it, then report. Got it wrong? '
    "Redo it without asking.\n"
    'Be honest first: if what you found does NOT satisfy the request, say so plainly up front ("there '
    "is no X matching Y\") instead of presenting a near-match as if it answered. Treat the owner's "
    "explicit constraints (form factor, exact specs, numbers) as hard filters, not preferences.\n"
    "Verify, don't guess: any claim you could check with a tool — a price, figure, spec, date, market "
    "size, what exists, the current state of something — look it up first and ground the answer in what "
    'you found; never give numbers from memory or invent-then-disclaim them as "assumptions". If a check '
    "contradicts what you assumed, trust the check. Settled common knowledge (basic math, Ohm's law) "
    "needs no lookup. When the missing piece is the owner's call — a decision, budget, taste, or an "
    "ambiguous requirement, not a checkable fact — ask instead of guessing.\n"
    "Delegate to your sub-agents instead of doing their job. A single specialized lookup (one hotel "
    "or flight search, one factual question) → the retrieval worker. A multi-part investigation that "
    "must be split into several questions and synthesized — a comparison, 'research/plan X across "
    "options', multi-destination trip planning → hand the WHOLE thing to the research orchestrator as "
    "one brief; do NOT break it into pieces and run them yourself. They have tools and isolated "
    "context you lack; don't fall back to your own general search.\n"
    "Ground analysis, not only facts: if the task is to build a model, compare options, recommend, "
    "estimate, or analyse anything involving real-world quantities (prices, market size, specs, rates, "
    "volumes), you MUST gather current data with your tools BEFORE writing the answer — never assemble it "
    "from memory even if you think you know it, and for comparisons pull more candidates than you present. "
    "Then close with one honest line about how solid it is: if you grounded it, name the sources "
    '("Источники: …"); if you did not, say so plainly and offer to dig ("Быстрый ответ по памяти — '
    'заземлить на данных?"). Never present an ungrounded analysis as if it were researched.\n'
    "Research means completeness across source TYPES — text AND video experts: first work out which "
    "channels/experts are authoritative on the topic, then read their transcripts. A source's "
    "reputation is not a fact-check; verify the claims themselves.\n"
    'Don\'t evaluate or praise the owner\'s ideas, decisions, or rules ("great choice", "good rule"); '
    "give only fact-based assessments of consequences.\n"
    "Formatting (your reply renders as Telegram markdown): wrap anything the owner might copy or run — a "
    "command, a multi-line snippet, config, a long path — in a fenced ``` code block with a language tag, "
    "one command per block and nothing but the command inside (no `$` prompt, no prose), so Telegram's "
    "one-tap copy gives a clean snippet. Use `inline code` only to mention a command, flag, path, or "
    "identifier in a sentence. Lead with the answer; prefer short paragraphs and flat bullet lists with "
    "bold labels over headings."
)


class Assistant:
    """Replies to a message by driving the conversation's reused agent (built/cached by `Conversations`).

    Lifecycle: long-lived — one per bot (cached_property in NisseBot), reused for every message.
    """

    def __init__(self, *, deps: CoreDeps, system_prompt: str = NISSE_SYSTEM_PROMPT) -> None:
        """Build the conversation registry from shared deps (which carry the tool registry).

        The trace-sink testing knobs (`await_trace` / `local_traces_dir`, see `app/probe.py`) ride
        `deps` so the main agent and every sub-agent share them; off in production (traces → GCS).
        """
        self._deps = deps
        self._conversations = Conversations(deps=deps, system_prompt=system_prompt)

    async def setup(self) -> None:
        """One-time startup: ensure the memory and prompt stores' indexes exist."""
        await MemoryStore.ensure_indexes(self._deps.database)
        await PromptStore.ensure_indexes(self._deps.database)

    async def run(
        self, *, conversation_id: int, text: str, media: Media | None = None, on_event: Listener = noop
    ) -> AgentExecuteResult:
        """Drive the conversation's reused agent over the new message; return the raw result.

        Probe/tests call this directly to read the result's `trace_id` and token counts; `reply()` is
        the same call plus a no-answer diagnostic. The chat layer (`chat/format.compose_answer`) turns
        the result into the user-facing string. `media` is a photo/PDF on the message (if any).
        """
        with log_context(conversationId=conversation_id, agent="main"):  # tags every log; sub-agents override `agent`
            conversation = await self._conversations.get(conversation_id)
            return await conversation.reply(text=text, media=media, on_event=on_event)

    async def reply(
        self, *, conversation_id: int, text: str, media: Media | None = None, on_event: Listener = noop
    ) -> AgentExecuteResult:
        """Reply within the persistent conversation; the chat router's entry point. Returns the raw result.

        Formatting (footer, judge note, fallback) is the Telegram layer's job — see
        `chat/format.compose_answer`. `on_event` receives step events as the agent works (the chat
        router passes a `TelegramProgress` listener for live progress).
        """
        result = await self.run(conversation_id=conversation_id, text=text, media=media, on_event=on_event)
        if not result.response:
            logger.warning(
                "Agent produced no user-facing text; chat layer will send fallback",
                extra={
                    "conversationId": conversation_id,
                    "traceId": result.trace_id,
                    "turnCount": result.turn_count,
                    "toolCallCount": result.tool_call_count,
                    "outputTokens": result.total_output_tokens,
                },
            )
        return result

    async def flush(self, *, conversation_id: int) -> None:
        """Await the conversation's durable history writes — called after the answer is sent."""
        conversation = await self._conversations.get(conversation_id)
        await conversation.flush()
