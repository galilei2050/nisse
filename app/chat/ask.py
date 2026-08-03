"""ask_user tool — a mid-turn clarifying question via a Telegram inline keyboard.

When one missing fact would change the answer, the agent calls `ask_user` instead of guessing or
spraying variants. To the agent it is an ordinary tool that returns the owner's choice; to the owner it
is a Telegram message with tappable options. The tool blocks the agent turn on an in-memory
`asyncio.Future` until the owner taps; the `callback_query` handler here (wired onto the chat router)
resolves it. There is exactly one process, one event loop (Cloud Run `max_instances=1`), so the tap and
the parked turn share the Future in memory — no queue, no cross-process sync. The handler resolves the
Future DIRECTLY, never through `assistant.reply()`, whose per-chat lock the parked turn still holds.
"""

import asyncio
import logging
import secrets
from dataclasses import dataclass, field

from aiogram import Bot, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from baski.agents.tool import Tool
from pydantic import BaseModel, Field

from app.shared import CoreDeps
from app.tools.registry import ToolRegistrar

logger = logging.getLogger(__name__)

_ANSWER_TIMEOUT = 300.0  # seconds to wait for a tap — well under the 1800s worker deadline
_CONFIRM_LABEL = "✅ Done"
_NONE_LABEL = "None of these"
_NONE_ANSWER = "The user indicated none of the options fit."
_TIMEOUT_ANSWER = "The user did not answer in time; proceed with a sensible default and note that you did."


class AskCallback(CallbackData, prefix="ask"):
    """Button payload for one ask_user keyboard: which question, which action, which option."""

    token: str  # keys the pending question (its Future lives under this token)
    action: str  # "pick" | "toggle" | "confirm" | "none"
    idx: int  # option index for pick/toggle; -1 for confirm/none


@dataclass(slots=True)
class _Pending:
    """One in-flight question: the Future the agent awaits, its options, and the live selection."""

    future: asyncio.Future[str]
    options: list[str]
    selected: set[int] = field(default_factory=set)


# token -> in-flight question. One per chat at a time (the per-chat lock serializes agent turns).
_pending: dict[str, _Pending] = {}


def _keyboard(token: str, options: list[str], selected: set[int], *, multi: bool) -> InlineKeyboardMarkup:
    """One button per option (a checkbox prefix when multi), plus a Done (multi only) + None row."""
    rows: list[list[InlineKeyboardButton]] = []
    for idx, label in enumerate(options):
        mark = ("✅ " if idx in selected else "☐ ") if multi else ""
        action = "toggle" if multi else "pick"
        data = AskCallback(token=token, action=action, idx=idx).pack()
        rows.append([InlineKeyboardButton(text=f"{mark}{label}", callback_data=data)])
    tail = [
        InlineKeyboardButton(text=_NONE_LABEL, callback_data=AskCallback(token=token, action="none", idx=-1).pack())
    ]
    if multi:
        confirm = AskCallback(token=token, action="confirm", idx=-1).pack()
        tail.insert(0, InlineKeyboardButton(text=_CONFIRM_LABEL, callback_data=confirm))
    rows.append(tail)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _selected_answer(options: list[str], selected: set[int]) -> str:
    """The tool-result string for a confirmed selection: the chosen labels."""
    return "The user selected: " + ", ".join(options[i] for i in sorted(selected))


class AskUserTool(Tool):
    """Ask the owner a clarifying question with tappable options; block until they answer.

    Lifecycle: per-conversation (built with the chat's bot + chat_id). Only built where a transport
    exists; the probe supplies a fake one that taps the first option, so probes exercise this tool.
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
        """Send the question, park on its Future until the owner taps, return their choice."""
        token = secrets.token_hex(4)
        pending = _Pending(future=asyncio.get_running_loop().create_future(), options=options)
        _pending[token] = pending
        message = await self._bot.send_message(
            chat_id=self._chat_id, text=question, reply_markup=_keyboard(token, options, set(), multi=multi)
        )
        logger.info("Asked user to clarify", extra={"options": len(options), "multi": multi})
        try:
            return await asyncio.wait_for(pending.future, timeout=_ANSWER_TIMEOUT)
        except TimeoutError:
            logger.warning("ask_user timed out — no tap arrived", extra={"timeout_s": _ANSWER_TIMEOUT})
            await message.delete()
            return _TIMEOUT_ANSWER
        finally:
            _pending.pop(token, None)


def resolve_pending(token: str, answer: str) -> None:
    """Deliver *answer* to the turn parked on *token*, without going through a Telegram tap.

    The seam the probe's fake transport uses so `ask_user` can be exercised off Telegram; a token
    whose question already timed out is simply gone.
    """
    pending = _pending.get(token)
    if pending is not None and not pending.future.done():
        pending.future.set_result(answer)


async def _handle_callback(query: CallbackQuery, callback_data: AskCallback) -> None:
    """Resolve or update one ask_user question from a button tap (wired on the chat router)."""
    pending = _pending.get(callback_data.token)
    logger.info("ask_user tap", extra={"action": callback_data.action, "known": pending is not None})
    if pending is None:  # stale tap — the question already resolved or timed out
        await query.answer()
        return
    if callback_data.action == "toggle":
        pending.selected ^= {callback_data.idx}
        await query.answer()
        if isinstance(query.message, Message):
            markup = _keyboard(callback_data.token, pending.options, pending.selected, multi=True)
            await query.message.edit_reply_markup(reply_markup=markup)
        return
    if callback_data.action == "confirm" and not pending.selected:
        await query.answer("Pick at least one option, or tap “None of these”.")
        return
    await _finish(query, pending, _resolve_answer(callback_data, pending))


def _resolve_answer(callback_data: AskCallback, pending: _Pending) -> str:
    """The tool-result string for a terminal tap (none / pick / confirm)."""
    if callback_data.action == "none":
        return _NONE_ANSWER
    if callback_data.action == "pick":
        return _selected_answer(pending.options, {callback_data.idx})
    return _selected_answer(pending.options, pending.selected)  # confirm


async def _finish(query: CallbackQuery, pending: _Pending, answer: str) -> None:
    """Deliver the answer to the parked agent turn and delete the question message."""
    if not pending.future.done():
        pending.future.set_result(answer)
    logger.info("ask_user resolved", extra={"chars": len(answer)})
    await query.answer()
    if isinstance(query.message, Message):
        await query.message.delete()


def register_ask_handler(router: Router) -> None:
    """Wire the ask_user button-tap handler onto the chat router.

    Taps arrive as callback_query updates — Telegram delivers them ONLY if the webhook's
    allowed_updates includes "callback_query" (baski derives that from the registered handlers). A
    webhook pinned to message-only silently drops every tap and the tool waits out its timeout.
    """
    router.callback_query.register(_handle_callback, AskCallback.filter())


def _ask_tools(deps: CoreDeps, conversation_id: int) -> list[Tool]:
    """The ask_user tool, bound to this chat — only where `deps.bot` carries a transport to ask over."""
    if deps.bot is None:
        return []
    return [AskUserTool(bot=deps.bot, chat_id=conversation_id)]


def register_tools(registrar: ToolRegistrar) -> None:
    """Register the ask_user clarifying-question tool."""
    registrar.register("ask_user", _ask_tools)
