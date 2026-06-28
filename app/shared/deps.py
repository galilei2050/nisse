"""CoreDeps — the shared low-level clients every domain's tools are built from."""

from dataclasses import dataclass

import httpx
from anthropic import AsyncAnthropic
from baski.clients.playwright_client import PlaywrightClient
from baski.clients.scheduler import Scheduler
from pymongo.asynchronous.database import AsyncDatabase


@dataclass(slots=True)
class CoreDeps:
    """Shared clients the conversation assembles every tool from.

    Lifecycle: long-lived, one per process (built once in `backend.py`). Holds the low-level clients
    that carry network connections + auth; a tool's per-conversation store/service is assembled from
    these in `Conversations._build_*_tools`. `scheduler` is always present — real `CloudTasksScheduler`
    in webhook mode, a `LoggingScheduler` in polling/probe — so no branch guards the scheduling tools.
    """

    http: httpx.AsyncClient
    anthropic: AsyncAnthropic
    database: AsyncDatabase
    playwright: PlaywrightClient
    bucket_name: str
    scheduler: Scheduler
    schedule_endpoint: str
