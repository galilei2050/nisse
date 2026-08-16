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
from typing import NamedTuple, cast

from baski.primitives import datetime
from pymongo.asynchronous.database import AsyncDatabase

from app.assistant.history import JUDGE_RETRY_PREFIX, TURNS_COLLECTION, StoredMessage, StoredTurn
from app.reactions.store import REACTIONS_COLLECTION
from app.scheduling.store import SCHEDULED_PREFIX

logger = logging.getLogger(__name__)

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
    owner_attached: bool = False  # a photo or PDF the owner sent; with no caption it is the whole ask

    @property
    def has_owner_input(self) -> bool:
        """Whether a human actually said something here — the only messages worth classifying."""
        return (bool(self.owner_text) or self.owner_attached) and not self.scheduled

    def render(self) -> str:
        """One exchange, labelled so a scheduled self-prompt is never mistaken for the owner talking."""
        header = f"[Turn {self.turn_id} · {datetime.as_utc(self.at).strftime('%Y-%m-%d %H:%M')} UTC]"
        if self.reactions:
            header += f"  reaction: {' '.join(self.reactions)}"
        if self.scheduled:
            header += "  (scheduled self-prompt — NOT the owner)"
        owner_label = "schedule" if self.scheduled else "owner"
        said = self.owner_text or ("(sent a photo or PDF, no caption)" if self.owner_attached else "(no text)")
        return f"{header}\n{owner_label}: {said}\nnisse: {self.answer_text or '(no answer)'}"


@dataclass
class PastRead:
    """One look backwards past the review window: what was folded, and what was actually read.

    The two are not the same and the difference is load-bearing. A read that lands entirely inside
    one question's tool rounds groups to nothing, and reported as a bare empty list that is
    indistinguishable from having reached the start of the conversation — so the pass would stop
    walking back exactly where the disputed exchange still lies ahead of it.
    """

    exchanges: list["Exchange"]
    oldest_turn_read: int | None  # None only when there was no turn older than the one asked about


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

    @property
    def has_owner_signal(self) -> bool:
        """Whether the owner said or reacted to anything — the precondition for a pass to be worth running.

        A window can be full of exchanges and still hold nothing to learn from: a scheduled check-in
        firing and the assistant answering itself is the whole of a night the owner slept through.
        Reactions count on their own, because an emoji with no message is the owner's cheapest signal.
        """
        return bool(self.owner_message_count or self.reaction_count)

    def render(self) -> str:
        """The digest the classifier and the curator both read — one block per exchange, oldest first."""
        if not self.exchanges:
            return "(no conversation in this window)"
        return "\n\n".join(exchange.render() for exchange in self.exchanges)


class TurnTexts(NamedTuple):
    """The two sides of one turn, flattened to plain text."""

    owner: str
    answer: str


class EvidenceCollector:
    """Reads the two collections a review window is assembled from.

    Answers both questions the nightly sweep asks of them: which chats were active, and what was said
    in one of them.

    Lifecycle: long-lived — one per `Curator`, and one more per built `transcript_read`, reused
    across conversations (the window and the chat are arguments to `collect`, the collections it
    reads are bound here).
    """

    def __init__(self, database: AsyncDatabase) -> None:
        """Bind the two collections a window is assembled from."""
        self._turns = database[TURNS_COLLECTION]
        self._reactions = database[REACTIONS_COLLECTION]

    async def active_conversations(self, *, since: datetime.datetime) -> list[int]:
        """Conversation ids with at least one turn since `since`, sorted."""
        ids = await self._turns.distinct("conversation_id", {"created_at": {"$gte": since}})
        logger.info("Curator sweep", extra={"conversations": len(ids)})
        return sorted(ids)

    async def collect(self, *, conversation_id: int, since: datetime.datetime) -> Evidence:
        """Every exchange since `since`, with the reactions that landed anywhere inside it.

        Soft-deleted turns are included: a turn the agent pruned from its context is still something
        the owner said, and a reaction can point at one.
        """
        docs = await self._turns.find(
            {"conversation_id": conversation_id, "created_at": {"$gte": since}}, sort=[("turn_id", 1)]
        ).to_list(length=None)
        exchanges = await self._exchanges(conversation_id=conversation_id, docs=cast("list[StoredTurn]", docs))

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

    async def before(self, *, conversation_id: int, turn_id: int, turns: int) -> "PastRead":
        """The exchanges folded from the `turns` turns immediately preceding `turn_id`, oldest first.

        The budget is in TURNS, not exchanges: one question spans a turn per tool round, so a fixed
        read yields anywhere from `turns` exchanges down to none — which is why the caller is told
        what was read and not only what was grouped.

        The window the pass reviews is a day, but a complaint about yesterday's answer routinely
        arrives after that answer has fallen out of it — and a complaint judged without the turn it
        disputes is how a permanent rule gets written on an unchecked premise (`BUGS.md` #18).
        Reaching back is deliberately a TOOL rather than a wider window: a wider one would re-review
        a day the previous pass already acted on, and the nightly schedule runs with no retries
        precisely because re-applied edits are the failure it cannot take back.
        """
        docs = await self._turns.find(
            {"conversation_id": conversation_id, "turn_id": {"$lt": turn_id}}, sort=[("turn_id", -1)]
        ).to_list(length=turns)
        docs.reverse()  # queried newest-first to take the `turns` nearest; read oldest-first
        exchanges = await self._exchanges(conversation_id=conversation_id, docs=cast("list[StoredTurn]", docs))
        logger.info(
            "Curator read past the window",
            extra={"conversationId": conversation_id, "beforeTurn": turn_id, "exchanges": len(exchanges)},
        )
        return PastRead(exchanges=exchanges, oldest_turn_read=docs[0]["turn_id"] if docs else None)

    async def _exchanges(self, *, conversation_id: int, docs: list[StoredTurn]) -> list[Exchange]:
        """Turns to exchanges: attach the reactions, fold, and cap the answers.

        Both read paths end here so the digest and the backwards reader cannot come to disagree
        about what an exchange is or how much of an answer one carries.
        """
        reactions = await self._reactions_by_turn(
            conversation_id=conversation_id, turn_ids=[doc["turn_id"] for doc in docs]
        )
        exchanges = self._group(docs, reactions)
        for exchange in exchanges:
            exchange.answer_text = exchange.answer_text[:_ANSWER_PREVIEW]
        return exchanges

    async def _reactions_by_turn(self, *, conversation_id: int, turn_ids: list[int]) -> dict[int, list[str]]:
        """The CURRENT emoji on each of `turn_ids`, from the reaction log.

        Keyed on the turns being reviewed, NOT on a time window: the owner reads an answer over coffee
        and taps it the next morning, so the tap and the turn fall in different nights. Windowing both
        would drop that reaction forever — the run holding the turn predates the tap, and the run
        holding the tap no longer collects the turn.

        The log is append-only and each record holds the whole new set, so the last record for a turn
        is its present state — an earlier 👍 that was taken back must not be read as still standing.
        """
        docs = await self._reactions.find(
            {"conversation_id": conversation_id, "turn_id": {"$in": turn_ids}}, sort=[("reacted_at", 1)]
        ).to_list(length=None)
        latest: dict[int, list[str]] = {}
        for doc in docs:
            latest[doc["turn_id"]] = list(doc["current"])
        return {turn_id: emoji for turn_id, emoji in latest.items() if emoji}

    @classmethod
    def _group(cls, docs: list[StoredTurn], reactions: dict[int, list[str]]) -> list[Exchange]:
        """Fold consecutive turns into exchanges: a turn carrying owner text opens one, the rest extend it.

        The LAST answer wins rather than all of them concatenated — the middle turns are the live
        progress narration ("смотрю списки"), and what the owner read and reacted to is the final reply.

        A judge retry is the loop talking to itself in the user role, so it EXTENDS the exchange it
        belongs to: the owner asked once, the judge sent the draft back, and the redone answer is the
        one that should hang off the owner's question. Opening a new exchange there would hand the
        curator a machine-written "correction" quoting the bot's own judge — the self-confirming loop
        its prompt forbids — and leave the owner's real question paired with the draft the judge
        rejected.
        """
        exchanges: list[Exchange] = []
        for doc in docs:
            messages = list(doc["messages"])
            texts = cls._turn_texts(messages)
            owner_text = "" if texts.owner.startswith(JUDGE_RETRY_PREFIX) else texts.owner
            attached = cls._attached_by_owner(messages)
            turn_id = doc["turn_id"]
            emoji = reactions.get(turn_id, [])
            if owner_text or attached or not exchanges:
                if not owner_text and not attached and not texts.answer:
                    continue  # a pure tool turn (or a judge retry) with nothing open to extend
                exchanges.append(
                    Exchange(
                        turn_id=turn_id,
                        at=doc["created_at"],
                        owner_text=owner_text,
                        answer_text=texts.answer,
                        reactions=list(emoji),
                        scheduled=owner_text.startswith(SCHEDULED_PREFIX),
                        owner_attached=attached,
                    )
                )
                continue
            current = exchanges[-1]
            if texts.answer:
                current.answer_text = texts.answer
            current.reactions.extend(e for e in emoji if e not in current.reactions)
        return exchanges

    @classmethod
    def _turn_texts(cls, messages: list[StoredMessage]) -> TurnTexts:
        """The owner's words and the bot's words in one turn."""
        owner = "\n".join(t for t in (cls._text_of(m, "user") for m in messages) if t).strip()
        answer = "\n".join(t for t in (cls._text_of(m, "assistant") for m in messages) if t).strip()
        return TurnTexts(owner=owner, answer=answer)

    @staticmethod
    def _attached_by_owner(messages: list[StoredMessage]) -> bool:
        """Whether the owner attached a photo or a PDF in this turn.

        A caption-less attachment IS the ask — it carries no text block, so counting words alone
        reads the turn as "the owner said nothing". `app/assistant/history.py` makes the same
        exception where it renders a turn for the judge.
        """
        for message in messages:
            content = message["content"]
            if message["role"] != "user" or isinstance(content, str):
                continue
            if any(block.get("type") in ("image", "document") for block in content):
                return True
        return False

    @staticmethod
    def _text_of(message: StoredMessage, role: str) -> str:
        """Concatenate the text blocks of one message of `role`; tool traffic contributes nothing."""
        if message["role"] != role:
            return ""
        content = message["content"]
        if isinstance(content, str):
            return content
        return "\n".join(block["text"] for block in content if block.get("type") == "text" and "text" in block)
