"""CoreDeps — the shared low-level clients every domain's tools are built from."""

from dataclasses import dataclass

import httpx
from anthropic import AsyncAnthropic
from baski.clients.playwright_client import PlaywrightClient
from baski.clients.scheduler import Scheduler
from baski.server import Logger
from pymongo.asynchronous.database import AsyncDatabase


@dataclass(slots=True)
class CoreDeps:
    """Shared clients the conversation assembles every tool from.

    Holds the low-level clients that carry network connections + auth, built once. A tool's
    per-conversation store/service is assembled from these in `Conversations._build_*_tools`.
    `scheduler` / `schedule_endpoint` are the Cloud Tasks wiring — both None in polling mode
    (no public fire callback), set together in webhook mode.
    """

    logger: Logger
    http: httpx.AsyncClient
    anthropic: AsyncAnthropic
    database: AsyncDatabase
    playwright: PlaywrightClient
    bucket_name: str
    scheduler: Scheduler | None
    schedule_endpoint: str | None
