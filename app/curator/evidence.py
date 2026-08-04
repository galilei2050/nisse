"""What the curator reviews: one window of conversation, with the owner's reactions attached.

Reading the raw transcript back is not enough to learn from it. The signal the owner gives cheapest
— a tap on a message — arrives as `(chat_id, message_id)` and only resolves to a turn because the
chat layer recorded which messages carried which answer. Joining the two here is what turns a pile
of emoji into "this specific answer earned a 👍".

**A turn is not an exchange.** The transcript stores one turn per API call, so a single question the
owner asked spans a turn for each tool round — the owner's words in the first, narration in the
middle, the real answer in the last. Handed over raw, most entries read as "owner said nothing" and
the day looks like the assistant talking to itself. So turns are grouped back into exchanges here:
one owner message, one answer, whatever reactions landed anywhere inside it.

Assembling the digest is deliberately plain code, not a model call: the curator should reason about
evidence, not spend a turn re-deriving what a Mongo query already knows.
"""

import logging
from dataclasses import dataclass, field
from typing import NamedTuple, TypedDict, cast

from baski.primitives import datetime
from pymongo.asynchronous.database import AsyncDatabase

from app.assistant.history import JUDGE_RETRY_PREFIX
from app.scheduling.store import SCHEDULED_PREFIX

logger = logging.getLogger(__name__)

_TURNS = "conversation_turns"
_REACTIONS = "reactions"
_ANSWER_PREVIEW = 900  # chars of the answer kept per exchange — enough to judge, not the whole essay


@dataclass
class Exchange:
    """One owner message and the answer it got, however many API turns that took."""

    turn_id: int  # the turn the owner's message opened — the id a reaction and the curator both name
    at: datetime.datetime
    owner_text: str
    answer_text: str
    reactions: list[str] = field(default_factory=list)  # emoji standing on any message of this exchange
    scheduled: bool = False  # the bot prompted itself (a reminder firing), so there is no owner input

    @property
    def has_owner_input(self) -> bool:
        """Whether a human actually said something here — the only messages worth classifying."""
        return bool(self.owner_text) and not self.scheduled


@dataclass
class Evidence:
    """One window of a conversation, ready to hand to the classifier and the curator."""

    conversation_id: int
    since: datetime.datetime
    exchanges: list[Exchange]

    @property
    def reaction_count(self) -> int:
        """How many exchanges carry a reaction — the feedback the owner gave for free."""
        return sum(1 for exchange in self.exchanges if exchange.reactions)

    @property
    def owner_message_count(self) -> int:
        """How many exchanges the owner actually opened, as opposed to a schedule firing."""
        return sum(1 for exchange in self.exchanges if exchange.has_owner_input)


class StoredBlock(TypedDict, total=False):
    """One content block as stored.

    Only the two keys this module reads are declared, and neither is required — a tool_use or image
    block legitimately carries neither.
    """

    type: str
    text: str


class StoredMessage(TypedDict):
    """One message as the transcript stored it — the history serialises every block to a plain dict.

    Deliberately not the SDK's `MessageParam`: what comes back from Mongo is JSON, and reading it as
    the type it actually has beats narrowing a union of twenty block shapes to find the text.
    """

    role: str
    content: str | list[StoredBlock]


class StoredTurn(TypedDict):
    """One turn document as `conversation_turns` holds it — the fields this module reads."""

    turn_id: int
    created_at: datetime.datetime
    messages: list[StoredMessage]


class TurnTexts(NamedTuple):
    """The two sides of one turn, flattened to plain text."""

    owner: str
    answer: str


def _text_of(message: StoredMessage, role: str) -> str:
    """Concatenate the text blocks of one message of `role`; tool traffic contributes nothing."""
    if message["role"] != role:
        return ""
    content = message["content"]
    if isinstance(content, str):
        return content
    return "\n".join(block["text"] for block in content if block.get("type") == "text" and "text" in block)


def _turn_texts(messages: list[StoredMessage]) -> TurnTexts:
    """The owner's words and the bot's words in one turn."""
    owner = "\n".join(t for t in (_text_of(m, "user") for m in messages) if t).strip()
    answer = "\n".join(t for t in (_text_of(m, "assistant") for m in messages) if t).strip()
    return TurnTexts(owner=owner, answer=answer)


def _group(docs: list[StoredTurn], reactions: dict[int, list[str]]) -> list[Exchange]:
    """Fold consecutive turns into exchanges: a turn carrying owner text opens one, the rest extend it.

    The LAST answer wins rather than all of them concatenated — the middle turns are the live
    progress narration ("смотрю списки"), and what the owner read and reacted to is the final reply.

    A judge retry is the loop talking to itself in the user role, so it EXTENDS the exchange it
    belongs to: the owner asked once, the judge sent the draft back, and the redone answer is the one
    that should hang off the owner's question. Opening a new exchange there would hand the curator a
    machine-written "correction" quoting the bot's own judge — the self-confirming loop its prompt
    forbids — and leave the owner's real question paired with the draft the judge rejected.
    """
    exchanges: list[Exchange] = []
    for doc in docs:
        texts = _turn_texts(list(doc["messages"]))
        owner_text = "" if texts.owner.startswith(JUDGE_RETRY_PREFIX) else texts.owner
        turn_id = doc["turn_id"]
        emoji = reactions.get(turn_id, [])
        if owner_text or not exchanges:
            if not owner_text and not texts.answer:
                continue  # a pure tool turn (or a judge retry) with nothing open to extend
            exchanges.append(
                Exchange(
                    turn_id=turn_id,
                    at=doc["created_at"],
                    owner_text=owner_text,
                    answer_text=texts.answer,
                    reactions=list(emoji),
                    scheduled=owner_text.startswith(SCHEDULED_PREFIX),
                )
            )
            continue
        current = exchanges[-1]
        if texts.answer:
            current.answer_text = texts.answer
        current.reactions.extend(e for e in emoji if e not in current.reactions)
    return exchanges


async def collect(database: AsyncDatabase, *, conversation_id: int, since: datetime.datetime) -> Evidence:
    """Every exchange since `since`, with the reactions that landed anywhere inside it.

    Soft-deleted turns are included: a turn the agent pruned from its context is still something the
    owner said, and a reaction can point at one.
    """
    docs = (
        await database[_TURNS]
        .find({"conversation_id": conversation_id, "created_at": {"$gte": since}}, sort=[("turn_id", 1)])
        .to_list(length=None)
    )
    reactions = await _reactions_by_turn(
        database, conversation_id=conversation_id, turn_ids=[doc["turn_id"] for doc in docs]
    )
    exchanges = _group(cast("list[StoredTurn]", docs), reactions)
    for exchange in exchanges:
        exchange.answer_text = exchange.answer_text[:_ANSWER_PREVIEW]

    evidence = Evidence(conversation_id=conversation_id, since=since, exchanges=exchanges)
    logger.info(
        "Curator evidence collected",
        extra={
            "conversationId": conversation_id,
            "turns": len(docs),
            "exchanges": len(exchanges),
            "ownerMessages": evidence.owner_message_count,
            "reactedExchanges": evidence.reaction_count,
        },
    )
    return evidence


async def _reactions_by_turn(
    database: AsyncDatabase, *, conversation_id: int, turn_ids: list[int]
) -> dict[int, list[str]]:
    """The CURRENT emoji on each of `turn_ids`, from the reaction log.

    Keyed on the turns being reviewed, NOT on a time window: the owner reads an answer over coffee
    and taps it the next morning, so the tap and the turn fall in different nights. Windowing both
    would drop that reaction forever — the run holding the turn predates the tap, and the run holding
    the tap no longer collects the turn.

    The log is append-only and each record holds the whole new set, so the last record for a turn is
    its present state — an earlier 👍 that was taken back must not be read as still standing.
    """
    docs = (
        await database[_REACTIONS]
        .find({"conversation_id": conversation_id, "turn_id": {"$in": turn_ids}}, sort=[("reacted_at", 1)])
        .to_list(length=None)
    )
    latest: dict[int, list[str]] = {}
    for doc in docs:
        latest[doc["turn_id"]] = list(doc["current"])
    return {turn_id: emoji for turn_id, emoji in latest.items() if emoji}


def render(evidence: Evidence) -> str:
    """The digest the classifier and the curator both read — one block per exchange, oldest first."""
    if not evidence.exchanges:
        return "(no conversation in this window)"
    return "\n\n".join(_render_exchange(exchange) for exchange in evidence.exchanges)


def _render_exchange(exchange: Exchange) -> str:
    """One exchange, labelled so a scheduled self-prompt is never mistaken for the owner talking."""
    header = f"[Turn {exchange.turn_id} · {datetime.as_utc(exchange.at).strftime('%Y-%m-%d %H:%M')} UTC]"
    if exchange.reactions:
        header += f"  reaction: {' '.join(exchange.reactions)}"
    if exchange.scheduled:
        header += "  (scheduled self-prompt — NOT the owner)"
    owner_label = "schedule" if exchange.scheduled else "owner"
    return (
        f"{header}\n{owner_label}: {exchange.owner_text or '(no text)'}\nnisse: {exchange.answer_text or '(no answer)'}"
    )
