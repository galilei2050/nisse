"""CoreDeps — the shared low-level clients every domain's tools are built from."""

from dataclasses import dataclass

import httpx
from anthropic import AsyncAnthropic
from baski.clients.playwright_client import PlaywrightClient
from baski.server import Logger
from pymongo.asynchronous.database import AsyncDatabase


@dataclass(slots=True)
class CoreDeps:
    """Shared clients passed to every domain tool provider.

    Holds only low-level, cross-domain clients. A domain builds its own mid-level
    clients (e.g. SerpApiClient, GmailClient) from these inside its provider.
    """

    logger: Logger
    http: httpx.AsyncClient
    anthropic: AsyncAnthropic
    database: AsyncDatabase
    playwright: PlaywrightClient
    bucket_name: str
