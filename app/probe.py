"""Manual probe — drive Assistant once, end-to-end, for ANY feature, and print the trace.

Builds `CoreDeps` directly (no NisseBot, no Telegram) and runs `Assistant.run()` with trace
persistence pointed at a local temp dir, then reads that trace to print the three things worth
checking on any change:
  1. INJECTED CONTEXT — the system prompt + the messages the agent received on its first turn.
     Confirm what reaches the model is what you expect.
  2. TOOL CALLS — every tool the agent invoked, with its arguments. Confirm the expected ones.
  3. ANSWER — the final reply.

Usage + expectation-first test cases: `app/CLAUDE.md` → "Manual probe", `docs/memory-test-cases.md`.

    python -m app.probe --user-id 1 --message "save that I love chocolate"
"""

import argparse
import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx
from anthropic import AsyncAnthropic
from baski.agents.trace import TraceRecord
from baski.clients.playwright_client import PlaywrightClient
from baski.env import get_env
from baski.server.logger import configure_logging
from pymongo import AsyncMongoClient

from app.assistant import Assistant
from app.browser import managed_browser_cdp_url
from app.chat.ask import PendingQuestions
from app.scheduling import LoggingScheduler
from app.shared import CoreDeps
from app.tools.wiring import build_tool_registry
from app.tracing import TraceView

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import InlineKeyboardMarkup
    from pymongo.asynchronous.database import AsyncDatabase


class _AutoTapQuestion:
    """Stands in for the sent question message, which `AskUserTool` deletes however the question ends."""

    async def delete(self) -> None:
        """Off Telegram there is nothing to take back."""


class _AutoTapBot:
    """Stands in for the Bot so `ask_user` runs in the probe, answering as the owner would.

    `CoreDeps` requires a transport, and measuring whether the agent CHOOSES to ask is the whole
    point of running it here. The real `AskUserTool` runs (its real schema and description reach the
    model); only the transport is faked, and every button goes through the run's own question
    registry, so a multi-select question takes the same toggle-then-Done path Telegram would drive.
    """

    def __init__(self, questions: PendingQuestions) -> None:
        """Tap through the same registry the run's `ask_user` parks its questions on."""
        self.asked: list[str] = []
        self._questions = questions

    async def send_message(
        self,
        *,
        chat_id: int,  # noqa: ARG002 — matches Bot.send_message, which the tool calls by keyword
        text: str,
        reply_markup: "InlineKeyboardMarkup",
    ) -> _AutoTapQuestion:
        """Walk the keyboard until a button settles the question — options first, then Done / None.

        Layout-agnostic on purpose: whatever `_Pending.keyboard` builds, the last row always ends a
        question. Which option a probe "chooses" doesn't matter — the count of questions asked is the
        measurement.
        """
        self.asked.append(text)
        buttons = [button for row in reply_markup.inline_keyboard for button in row]
        if not any(self._questions.resolve_tap(str(button.callback_data)) for button in buttons):
            raise RuntimeError("probe tapped every button and the question stayed open")  # a harness bug
        return _AutoTapQuestion()


async def _run(user_id: int, message: str, traces_dir: Path) -> None:
    async with AsyncExitStack() as resources:
        http = await resources.enter_async_context(httpx.AsyncClient(timeout=httpx.Timeout(timeout=30.0)))
        cdp_url = managed_browser_cdp_url()
        playwright = await resources.enter_async_context(PlaywrightClient(headless=True, cdp_url=cdp_url))
        database: AsyncDatabase = AsyncMongoClient(str(get_env("MONGODB_URI")), tz_aware=True).get_default_database()
        questions = PendingQuestions()
        auto_tap = _AutoTapBot(questions)
        deps = CoreDeps(
            http=http,
            anthropic=AsyncAnthropic(api_key=str(get_env("ANTHROPIC_API_KEY")), timeout=600.0),
            database=database,
            playwright=playwright,
            bucket_name=str(get_env("PRIVATE_BUCKET_NAME")),
            scheduler=LoggingScheduler(),  # probe has no Cloud Tasks — log the enqueue instead
            schedule_endpoint="http://localhost/schedule/fire",
            browser_cdp_url=cdp_url,
            judge_project=str(get_env("GOOGLE_CLOUD_PROJECT")),
            tools=build_tool_registry(),
            local_traces_dir=str(traces_dir),  # main agent + sub-agents write here; probe reads it after
            await_trace=True,
            bot=cast("Bot", auto_tap),  # transport stand-in: only send_message is ever called on it
            questions=questions,
        )
        assistant = Assistant(deps=deps)
        await assistant.setup()
        result = (await assistant.run(conversation_id=user_id, text=message)).result
        await assistant.flush(conversation_id=user_id)  # persist turn writes + soft-deletes, as prod does post-send

    trace = TraceRecord.model_validate_json((traces_dir / f"{result.trace_id}.json").read_text())
    TraceView(trace, system=True).print_report()  # re-inspect any saved run with `python -m app.tracing`
    print(f"\n=== ASKED THE OWNER === {len(auto_tap.asked)}")  # the questions themselves are in TOOL CALLS
    print(f"\n=== TRACE SAVED ===\n{result.trace_id}")
    print(f"inspect: uv run python -m app.tracing {result.trace_id} --results [--grep TEXT] [--system] [--full]")


def main() -> None:
    """Parse CLI args and run one probe; the trace is saved under scratch/traces/ for `app.trace`."""
    parser = argparse.ArgumentParser(description="Drive Assistant.run() once for manual end-to-end testing.")
    parser.add_argument("--user-id", type=int, default=1, help="Conversation id (acts as the owner's chat id)")
    parser.add_argument("--message", required=True, help="Text to send to the agent")
    args, _ = parser.parse_known_args()
    configure_logging(cloud=False, debug=False)  # readable logs carrying ambient labels (conversationId)
    traces_dir = Path("scratch/traces")  # persist so the trace can be ANALYSED separately (no re-run)
    traces_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(_run(args.user_id, args.message, traces_dir))


if __name__ == "__main__":
    main()
