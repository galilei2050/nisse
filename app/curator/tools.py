"""Reading the transcript past the window the pass reviews — the curator's one look backwards.

The pass is handed a day, and what that budget costs is the other side of a complaint. The owner
objects to yesterday's answer after that answer has aged out, the pass sees only the objection, and
"quote the owner" is satisfied by a complaint that is itself wrong — which is how two permanent judge
rules were written on a premise nobody checked (`BUGS.md` #18). Reading on demand closes that without
widening anything: the pass reaches for the disputed turn when a complaint names one, and reads
nothing extra when it does not. Why a tool and not a wider window: `EvidenceCollector.before`.
"""

import logging

from baski.agents import Tool
from pydantic import BaseModel, Field

from app.curator.evidence import EvidenceCollector, Exchange
from app.shared import CoreDeps
from app.tools.registry import ToolRegistrar

logger = logging.getLogger(__name__)

# One page per call, and the pass reaches further by lowering `before_turn_id` rather than by asking
# for more. Ten because turns are not exchanges: the 15.08 case needed four exchanges but ten turns to
# reach, and a page that stops short costs a whole extra round-trip to discover it did.
_READ_BACK = 10


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
        """Where to read back from. How far is fixed — call again, lower, to go further."""

        before_turn_id: int = Field(
            description="Read the exchanges preceding this turn id. Use the oldest [Turn N] in the digest."
        )

    def __init__(self, evidence: EvidenceCollector, *, conversation_id: int) -> None:
        """Bind to the collector that already folds turns into exchanges, and to this chat."""
        self._evidence = evidence
        self._conversation_id = conversation_id

    async def execute(self, before_turn_id: int) -> str:
        """The exchanges before `before_turn_id`, rendered as the digest renders them."""
        read = await self._evidence.before(
            conversation_id=self._conversation_id, turn_id=before_turn_id, turns=_READ_BACK
        )
        if read.oldest_turn_read is None:
            return f"Nothing before turn {before_turn_id} — that is the start of this conversation."
        if not read.exchanges:
            # Every turn read was a tool round of a question that opens further back. Saying "nothing
            # here" would stop the walk one call short of the exchange it was sent to find.
            return (
                f"Turns {read.oldest_turn_read}–{before_turn_id - 1} are the tool rounds of an exchange "
                f"that opens earlier. Call again with before_turn_id={read.oldest_turn_read}."
            )
        return "\n\n".join([*self._cut_note(read.exchanges[0]), *(exchange.render() for exchange in read.exchanges)])

    @staticmethod
    def _cut_note(first: Exchange) -> list[str]:
        """A warning when the oldest exchange returned was opened before the read began.

        An exchange spans several turns, so reading back a fixed number of them lands mid-exchange
        about as often as not, and the owner's message that opened it stays behind the cut. Rendered
        without a word, that reads as "(no text)" — the owner said nothing — which is the exact
        misreading this tool exists to prevent.
        """
        if first.has_owner_input or first.scheduled:
            return []
        return [f"(turn {first.turn_id} is cut off here — what the owner said to open it is older; read further back)"]


def transcript_tools(deps: CoreDeps, conversation_id: int) -> list[Tool]:
    """The backwards reader over one chat's transcript (curator-only roster)."""
    return [TranscriptReadTool(EvidenceCollector(deps.database), conversation_id=conversation_id)]


def register_tools(registrar: ToolRegistrar) -> None:
    """Register the reader under the name the curator's tool spec references."""
    registrar.register("transcript", transcript_tools)
