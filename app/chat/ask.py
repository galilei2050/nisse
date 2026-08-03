"""ask_user tool — a mid-turn clarifying question via a Telegram inline keyboard.

When one missing fact would change the answer, the agent calls `ask_user` instead of guessing or
spraying variants. To the agent it is an ordinary tool that returns the owner's choice; to the owner it
is a Telegram message with tappable options. The tool blocks the agent turn on an in-memory
`asyncio.Future` until the owner taps; the `callback_query` handler here (wired onto the chat router)
resolves it — as does a typed reply, which the chat router hands to `answer_pending` before it would
start a new turn. There is exactly one process, one event loop (Cloud Run `max_instances=1`), so the
answer and the parked turn share the Future in memory — no queue, no cross-process sync. Both paths
resolve the Future DIRECTLY, never through `assistant.reply()`, whose per-chat lock the parked turn
still holds — routing an answer through it would deadlock until the question expired.
"""

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from enum import StrEnum

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from baski.agents.tool import Tool
from pydantic import BaseModel, Field

from app.shared import CoreDeps
from app.tools.registry import ToolRegistrar

logger = logging.getLogger(__name__)

_ANSWER_TIMEOUT = 300.0  # seconds to wait for an answer — well under the 1800s worker deadline
_CONFIRM_LABEL = "✅ Done"
_NONE_LABEL = "None of these"
_NONE_ANSWER = "The user indicated none of the options fit."
_TIMEOUT_ANSWER = "The user did not answer. Do NOT invent a value for them — say what you still need."
_ALREADY_ASKING = "A question of yours is still unanswered — wait for it before asking another."


class _Action(StrEnum):
    """What a button does when tapped, packed into its callback payload."""

    PICK = "pick"  # single choice: this option IS the answer
    TOGGLE = "toggle"  # multi: flip this option, keep waiting
    CONFIRM = "confirm"  # multi: answer with everything picked so far
    NONE = "none"  # none of the options fit


class AskCallback(CallbackData, prefix="ask"):
    """Button payload for one ask_user keyboard: which question, which action, which option."""

    token: str  # keys the pending question (its Future lives under this token)
    action: _Action
    idx: int  # option index for pick/toggle; -1 for confirm/none


class _Tap(StrEnum):
    """What one tap did to its question — Telegram renders it, the probe only checks for ANSWERED."""

    ANSWERED = "answered"  # the parked agent turn now has its answer
    TOGGLED = "toggled"  # multi: selection flipped, still waiting for Done
    EMPTY = "empty"  # multi: Done pressed with nothing picked
    STALE = "stale"  # the question already resolved or timed out


@dataclass(slots=True)
class _Pending:
    """One in-flight question: the Future the agent awaits, its options, and the live selection."""

    future: asyncio.Future[str]
    options: list[str]
    chat_id: int  # so a TYPED answer from that chat can reach this question (see `answer_pending`)
    selected: set[int] = field(default_factory=set)


# token -> in-flight question. At most one per chat, enforced by `execute` — `answer_pending` routes a
# typed answer by chat alone, so a second open question in the same chat would make it a coin flip.
_pending: dict[str, _Pending] = {}


def _keyboard(token: str, options: list[str], selected: set[int], *, multi: bool) -> InlineKeyboardMarkup:
    """One button per option (a checkbox prefix when multi), plus a Done (multi only) + None row."""
    rows: list[list[InlineKeyboardButton]] = []
    for idx, label in enumerate(options):
        mark = ("✅ " if idx in selected else "☐ ") if multi else ""
        action = _Action.TOGGLE if multi else _Action.PICK
        data = AskCallback(token=token, action=action, idx=idx).pack()
        rows.append([InlineKeyboardButton(text=f"{mark}{label}", callback_data=data)])
    none = AskCallback(token=token, action=_Action.NONE, idx=-1).pack()
    tail = [InlineKeyboardButton(text=_NONE_LABEL, callback_data=none)]
    if multi:
        confirm = AskCallback(token=token, action=_Action.CONFIRM, idx=-1).pack()
        tail.insert(0, InlineKeyboardButton(text=_CONFIRM_LABEL, callback_data=confirm))
    rows.append(tail)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _selected_answer(options: list[str], selected: set[int]) -> str:
    """The tool-result string for a confirmed selection: the chosen labels."""
    return "The user selected: " + ", ".join(options[i] for i in sorted(selected))


class AskUserTool(Tool):
    """Ask the owner a clarifying question with tappable options; block until they answer.

    Lifecycle: per-conversation (built with the chat's bot + chat_id).
    """

    name = "ask_user"
    one_line = "Ask the owner a short multiple-choice question and wait for their tap"
    description = (
        "Ask the owner ONE question only they can answer — a budget, a taste, a date or time they "
        "never gave, which of several targets they mean — when guessing it would change the answer. "
        "Shows tappable options and returns their choice. Call it BEFORE doing the work, never after; "
        "a question left in your reply text instead of here is a failure. Not for permission to act, "
        "and not for anything you could look up yourself."
    )

    class Input(BaseModel):
        """One clarifying question and its tappable options."""

        question: str = Field(description="The clarifying question, in the user's language")
        options: list[str] = Field(min_length=2, description="2-6 short answer choices to tap")
        multi: bool = Field(default=False, description="True lets the user pick several (adds a Done button)")

    def __init__(self, *, bot: Bot, chat_id: int) -> None:
        """Hold the chat's bot and the id the question is sent to."""
        self._bot = bot
        self._chat_id = chat_id

    async def execute(self, *, question: str, options: list[str], multi: bool = False) -> str:
        """Send the question, park on its Future until the owner taps or types an answer, return it."""
        if any(p.chat_id == self._chat_id for p in _pending.values()):
            return _ALREADY_ASKING  # the model may emit two ask_user calls in one turn; baski runs them together
        token = secrets.token_hex(4)
        pending = _Pending(future=asyncio.get_running_loop().create_future(), options=options, chat_id=self._chat_id)
        _pending[token] = pending  # before the send, so nothing may await between the check above and here
        try:
            message = await self._bot.send_message(
                chat_id=self._chat_id, text=question, reply_markup=_keyboard(token, options, set(), multi=multi)
            )
        except Exception:
            del _pending[token]  # the question never reached the owner; an orphan would eat their next message
            raise
        logger.info("Asked user to clarify", extra={"options": len(options), "multi": multi})
        try:
            return await asyncio.wait_for(pending.future, timeout=_ANSWER_TIMEOUT)
        except TimeoutError:
            logger.warning("ask_user timed out", extra={"timeout_s": _ANSWER_TIMEOUT})
            return _TIMEOUT_ANSWER
        finally:
            _pending.pop(token)
            await _discard(message)


async def _discard(message: Message) -> None:
    """Take the spent keyboard off the screen — however the question ended.

    Runs on the answered path too, inside the agent turn's own `finally`: a refused deleteMessage
    must not be allowed to escape and turn an answered question into a failed turn.
    """
    try:
        await message.delete()
    except TelegramAPIError as exc:
        logger.warning("ask_user could not delete its question", extra={"error": str(exc)})


def _apply_tap(data: AskCallback) -> _Tap:
    """Apply one button tap to its question; the whole tap state machine lives here.

    Telegram is not the only thing that taps — the probe drives the same transitions through
    `resolve_tap`, so both see identical answer strings and identical multi-select behaviour.
    """
    pending = _pending.get(data.token)
    if pending is None or pending.future.done():
        return _Tap.STALE
    if data.action is _Action.TOGGLE:
        pending.selected ^= {data.idx}
        return _Tap.TOGGLED
    if data.action is _Action.CONFIRM and not pending.selected:
        return _Tap.EMPTY
    pending.future.set_result(_resolve_answer(data, pending))
    return _Tap.ANSWERED


def resolve_tap(callback_data: str) -> bool:
    """Tap a button that did not arrive over Telegram; True once the question is answered.

    Takes the raw payload the button carries, so the caller needs to know nothing about options,
    indices, or how an answer is worded — a toggle selects and keeps waiting, Done resolves.
    """
    return _apply_tap(AskCallback.unpack(callback_data)) is _Tap.ANSWERED


def answer_pending(*, chat_id: int, text: str) -> bool:
    """Deliver a TYPED reply to that chat's live question; True if one was waiting for it.

    The owner answers "в 9 утра" as often as they tap. Without this the message queues behind the
    parked turn's per-chat lock and the question expires, handing the agent the timeout string
    instead of the answer the owner had already given it.
    """
    for pending in _pending.values():
        if pending.chat_id == chat_id and not pending.future.done():
            pending.future.set_result(f"The user answered: {text}")
            return True
    return False


async def _handle_callback(query: CallbackQuery, callback_data: AskCallback) -> None:
    """Resolve or update one ask_user question from a button tap (wired on the chat router)."""
    outcome = _apply_tap(callback_data)
    logger.info("ask_user tap", extra={"action": callback_data.action, "outcome": str(outcome)})
    if outcome is _Tap.EMPTY:
        await query.answer("Pick at least one option, or tap “None of these”.")
        return
    await query.answer()
    # Re-read AFTER that round trip: the question may have expired while it was in flight.
    pending = _pending.get(callback_data.token)
    if outcome is _Tap.TOGGLED and pending is not None and isinstance(query.message, Message):
        markup = _keyboard(callback_data.token, pending.options, pending.selected, multi=True)
        await query.message.edit_reply_markup(reply_markup=markup)


def _resolve_answer(callback_data: AskCallback, pending: _Pending) -> str:
    """The tool-result string for a terminal tap (none / pick / confirm)."""
    if callback_data.action == "none":
        return _NONE_ANSWER
    if callback_data.action == "pick":
        return _selected_answer(pending.options, {callback_data.idx})
    return _selected_answer(pending.options, pending.selected)  # confirm


def register_ask_handler(router: Router) -> None:
    """Wire the ask_user button-tap handler onto the chat router.

    Taps arrive as callback_query updates — Telegram delivers them ONLY if the webhook's
    allowed_updates includes "callback_query" (baski derives that from the registered handlers). A
    webhook pinned to message-only silently drops every tap and the tool waits out its timeout.
    """
    router.callback_query.register(_handle_callback, AskCallback.filter())


def _ask_tools(deps: CoreDeps, conversation_id: int) -> list[Tool]:
    return [AskUserTool(bot=deps.bot, chat_id=conversation_id)]


def register_tools(registrar: ToolRegistrar) -> None:
    """Register the ask_user clarifying-question tool."""
    registrar.register("ask_user", _ask_tools)
