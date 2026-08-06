"""CoreDeps — the shared clients and services every tool is built from."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from aiogram import Bot
from anthropic import AsyncAnthropic
from baski.clients.playwright_client import PlaywrightClient
from baski.clients.scheduler import Scheduler
from pymongo.asynchronous.database import AsyncDatabase

if TYPE_CHECKING:
    from app.chat.ask import PendingQuestions
    from app.tools import ToolRegistry


@dataclass(slots=True)
class CoreDeps:
    """Shared clients + services the conversation assembles every tool from.

    Lifecycle: long-lived, one per process (built once in `backend.py`). Holds the low-level clients
    that carry network connections + auth, plus the process-wide services built at startup
    (`scheduler`, the tool `registry`); a tool's per-conversation store is assembled from these when the
    registry builds it. `scheduler` is always present — real `CloudTasksScheduler` in webhook mode, a
    `LoggingScheduler` in polling/probe — so no branch guards the scheduling tools.

    The completeness judge is NOT here: half its rubric is a per-conversation document, so it is built
    per conversation (`app/assistant/judge.py`) rather than once for the process.
    """

    http: httpx.AsyncClient
    anthropic: AsyncAnthropic
    database: AsyncDatabase
    playwright: PlaywrightClient
    bucket_name: str
    scheduler: Scheduler
    schedule_endpoint: str
    tools: "ToolRegistry"  # the process-wide name→factory tool catalog (app/tools)
    bot: Bot  # transport for tools that talk to the owner directly (ask_user); the probe fakes one
    # Where `ask_user` parks a question and where the chat router looks for one to answer — the two
    # sides only meet if they hold the SAME registry, so it is owned here rather than as a global.
    questions: "PendingQuestions"
    # Trace sink for main agent + every sub-agent; probe sets both to read the whole chain locally.
    local_traces_dir: str | None = None  # None → GCS
    await_trace: bool = False
