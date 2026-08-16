"""Reading the transcript past the window the pass reviews — the curator's one look backwards.

The pass is handed a day. That is a deliberate budget, not an oversight: a wider window means
re-reviewing a day the previous pass already acted on, and the nightly schedule carries no retries
because re-applied edits are the one failure it cannot take back.

What the budget costs is the other side of a complaint. The owner objects to yesterday's answer
after that answer has aged out, the pass sees only the objection, and "quote the owner" is satisfied
by a complaint that is itself wrong — which is how two permanent judge rules were written on a
premise nobody checked (`BUGS.md` #18). A tool closes that without widening anything: the pass
reaches for the disputed turn when a complaint names one, and reads nothing extra when it does not.
"""

import logging

from baski.agents import Tool
from pydantic import BaseModel, Field

from app.curator.evidence import EvidenceCollector, Exchange
from app.shared import CoreDeps
from app.tools.registry import ToolRegistrar

logger = logging.getLogger(__name__)

TRANSCRIPT_TOOL_NAME = "transcript"

_DEFAULT_LIMIT = 5
_MAX_LIMIT = 20  # a page of history at a time; the pass reads back to a turn, it does not re-read days


class TranscriptReadTool(Tool):
    """Reads exchanges older than the review window. Lifecycle: per-conversation."""

    name = "transcript_read"
    one_line = "Read exchanges from before the window you are reviewing"
    description = (
        "Read the exchanges immediately BEFORE a turn, including ones older than your window. Use it "
        "when the owner complains about an answer you cannot see: his complaint is evidence that he "
        "was unhappy, never evidence that what he says happened is what happened. Start from the "
        "oldest turn id in the digest and walk back until you find the exchange being disputed. "
        "Returns the owner's words, the answer, and any reactions — the same shape as the digest."
    )

    class Input(BaseModel):
        """Where to read back from, and how far."""

        before_turn_id: int = Field(
            description="Read the exchanges preceding this turn id. Use the oldest [Turn N] in the digest."
        )
        limit: int = Field(
            default=_DEFAULT_LIMIT,
            description=f"How many exchanges to return, oldest first (max {_MAX_LIMIT}).",
        )

    def __init__(self, evidence: EvidenceCollector, *, conversation_id: int) -> None:
        """Bind to the collector that already folds turns into exchanges, and to this chat."""
        self._evidence = evidence
        self._conversation_id = conversation_id

    async def execute(self, before_turn_id: int, limit: int = _DEFAULT_LIMIT) -> str:
        """The exchanges before `before_turn_id`, rendered as the digest renders them."""
        exchanges = await self._evidence.before(
            conversation_id=self._conversation_id,
            turn_id=before_turn_id,
            limit=min(max(limit, 1), _MAX_LIMIT),
        )
        if not exchanges:
            return f"Nothing before turn {before_turn_id} — that is the start of this conversation."
        return "\n\n".join([*self._cut_note(exchanges[0]), *(exchange.render() for exchange in exchanges)])

    @staticmethod
    def _cut_note(first: Exchange) -> list[str]:
        """A warning when the oldest exchange returned was opened before the read began.

        An exchange spans several turns, so reading back a fixed number of them lands mid-exchange
        about as often as not, and the owner's message that opened it stays behind the cut. Rendered
        without a word, that reads as "(no text)" — the owner said nothing — which is the exact
        misreading this tool exists to prevent.
        """
        if first.owner_text or first.scheduled:
            return []
        return [f"(turn {first.turn_id} is cut off here — what the owner said to open it is older; read further back)"]


def transcript_tools(deps: CoreDeps, conversation_id: int) -> list[Tool]:
    """The backwards reader over one chat's transcript (curator-only roster)."""
    return [TranscriptReadTool(EvidenceCollector(deps.database), conversation_id=conversation_id)]


def register_tools(registrar: ToolRegistrar) -> None:
    """Register the reader under the name the curator's tool spec references."""
    registrar.register(TRANSCRIPT_TOOL_NAME, transcript_tools)
