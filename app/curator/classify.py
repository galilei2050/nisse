"""Classifying what the owner's messages were DOING — the curator's evidence, not a router.

A label per owner message: was that a task, a correction, a standing instruction, or just talk?
Without it the curator reads a wall of transcript and reaches for whatever is most recent; with it,
"the owner pushed back three times about X" is a countable fact.

**Offline, once a night — never on the request path.** An inline classifier on a single-user bot is
a rejected direction (`app/CLAUDE.md`: no cost/latency machinery without amortization): it would tax
every message to serve a nightly consumer. Here the whole window costs one call.

The taxonomy is not invented. It follows the implicit-feedback ontology of Don-Yehiya et al. (2024),
as used and densely re-annotated by Liu, Zhang & Choi, *User Feedback in Human-LLM Dialogues*
(arXiv:2507.23158): positive feedback plus four negative kinds — rephrasing, make-aware-without-
correction, make-aware-with-correction, and ask-for-clarification. Three labels are added for what
this bot must route on and a *feedback* ontology has no slot for: a plain request (a message
carrying no verdict at all), a standing instruction, and pure talk.

Three findings from that paper shape how the output may be used, and they are why nothing here
triggers a change on its own:

1. **Praise is a bad learning signal.** Prompts that drew positive feedback scored slightly LOWER on
   quality and higher on toxicity — users praise the model most when it went along with a bad
   request. A `praise` label is therefore evidence of nothing beyond mood.
2. **The content beats the polarity.** What was unsatisfactory teaches; "thumbs down" does not. So
   every label carries the owner's own words and a note on what was actually wrong.
3. **Automatic labelling is noisy** — ~49% accuracy on the fine-grained set in that study. A label is
   a lead to verify against the transcript, never a fact to act on.
"""

import json
import logging
from enum import StrEnum

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field, ValidationError

from app.curator.evidence import Evidence, render

logger = logging.getLogger(__name__)

CLASSIFIER_MODEL = "claude-sonnet-5"  # a labelling pass over one day; the reasoning happens in the curator
# One entry per owner message, quote included. A busy day runs to dozens; at 4096 a real day came back
# truncated mid-string, which surfaces as "the classifier returned garbage" rather than as a token cap.
_MAX_TOKENS = 16_384


class MessageKind(StrEnum):
    """What one owner message was doing.

    The four negative kinds are the ontology's, kept distinct because they call for different
    responses: a rephrase means the answer missed the ask silently, a correction carries the fix, a
    rejection carries only the miss.
    """

    REQUEST = "request"  # a task or question, carrying no feedback on the previous answer
    PRAISE = "praise"  # positive feedback — mood, not evidence of quality (see module docstring)
    REPHRASE = "rephrase"  # re-asked the same thing differently: the answer missed, silently
    REJECTION = "rejection"  # "that's wrong / not what I asked", with no fix supplied
    CORRECTION = "correction"  # said what was wrong AND what right looks like — the richest signal
    CLARIFICATION = "clarification"  # asked for something the answer should have contained
    DIRECTIVE = "directive"  # a standing rule for how to behave from now on, not about one answer
    SOCIAL = "social"  # venting, thinking aloud, chat — no task and no verdict


class MessageSignal(BaseModel):
    """One classified owner message, tied to the turn it came from and quoted from the transcript."""

    turn_id: int
    kind: MessageKind
    quote: str = Field(description="The owner's own words that justify the label — verbatim, short.")
    about: str = Field(description="What it concerns: the topic, or what was wrong with the answer.")


class Classification(BaseModel):
    """The classifier's whole output for one window."""

    signals: list[MessageSignal]


_INSTRUCTIONS = (
    "You label what the OWNER's messages in a chat transcript were doing. You are building evidence "
    "for a maintenance pass; you change nothing.\n\n"
    "Label every owner message with exactly one kind:\n"
    "- request: a task or question. Carries no verdict on the previous answer. Most messages.\n"
    "- praise: approves the previous answer ('отлично', 'спасибо, то что нужно').\n"
    "- rephrase: asks the SAME thing again, worded differently — the previous answer missed and the "
    "owner is retrying rather than complaining.\n"
    "- rejection: says the answer was wrong/irrelevant WITHOUT saying what right would be.\n"
    "- correction: says what was wrong AND supplies the fix, the missing constraint, or the right "
    "value. Frustration ('опять ты...', 'я же просил') belongs here when a fix is implied.\n"
    "- clarification: asks for something the previous answer should have included.\n"
    "- directive: a standing rule for future behaviour ('всегда отвечай по-русски', 'не спрашивай "
    "разрешения') — about how to act from now on, not about one answer.\n"
    "- social: venting, thinking aloud, greetings, personal talk with no task.\n\n"
    "Rules:\n"
    "- Label ONLY messages shown as `owner:`. A block marked `schedule:` is the bot prompting itself "
    "on a timer — skip it entirely, it is not the owner speaking.\n"
    "- The FIRST message of a topic cannot be feedback — there is nothing before it to judge.\n"
    "- Prefer the more specific kind: correction over rejection when a fix is present; directive over "
    "correction when the rule outlives this one answer.\n"
    "- `quote` must be the owner's exact words, trimmed to the part that carries the signal.\n"
    "- `about` must name the subject concretely — 'wanted prices in the comparison', not 'the answer'.\n"
    "- Skip nothing and invent nothing: one entry per owner message that has text.\n\n"
    'Reply with JSON only: {"signals": [{"turn_id": 12, "kind": "correction", "quote": "...", '
    '"about": "..."}]}'
)


async def classify(anthropic: AsyncAnthropic, evidence: Evidence) -> Classification:
    """Label every owner message in the window. Raises if the model returns unusable JSON.

    Failing loud is right here: a curator running on a silently-empty classification would look like
    a quiet night rather than a broken pass.
    """
    message = await anthropic.messages.create(
        model=CLASSIFIER_MODEL,
        max_tokens=_MAX_TOKENS,
        system=_INSTRUCTIONS,
        messages=[{"role": "user", "content": render(evidence)}],
    )
    raw = "".join(block.text for block in message.content if block.type == "text").strip()
    classification = _parse(raw)
    logger.info(
        "Owner messages classified",
        extra={
            "conversationId": evidence.conversation_id,
            "signals": len(classification.signals),
            "kinds": sorted({s.kind for s in classification.signals}),
        },
    )
    return classification


def _parse(raw: str) -> Classification:
    """Parse the model's JSON, tolerating a ```json fence but nothing looser."""
    text = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return Classification.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"classifier returned unusable output: {text[:400]}") from exc
