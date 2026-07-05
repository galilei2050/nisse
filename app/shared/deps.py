"""CoreDeps — the shared clients and services every tool is built from."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from anthropic import AsyncAnthropic
from baski.agents import Judge
from baski.clients.playwright_client import PlaywrightClient
from baski.clients.scheduler import Scheduler
from pymongo.asynchronous.database import AsyncDatabase

if TYPE_CHECKING:
    from app.tools import ToolRegistry


@dataclass(slots=True)
class CoreDeps:
    """Shared clients + services the conversation assembles every tool from.

    Lifecycle: long-lived, one per process (built once in `backend.py`). Holds the low-level clients
    that carry network connections + auth, plus the process-wide services built at startup (`judge`,
    `scheduler`, the tool `registry`); a tool's per-conversation store is assembled from these when the
    registry builds it. `scheduler` is always present — real `CloudTasksScheduler` in webhook mode, a
    `LoggingScheduler` in polling/probe — so no branch guards the scheduling tools.
    """

    http: httpx.AsyncClient
    anthropic: AsyncAnthropic
    database: AsyncDatabase
    playwright: PlaywrightClient
    bucket_name: str
    scheduler: Scheduler
    schedule_endpoint: str
    judge: Judge  # cross-family completeness judge; the agent runs it at the loop's exit and retries
    tools: "ToolRegistry"  # the process-wide name→factory tool catalog (app/tools)
