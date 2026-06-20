"""TelegramServer entry point — builds shared deps + assistant and mounts the chat router."""

import logging
import sys
from collections.abc import Iterable
from contextlib import AsyncExitStack
from functools import cached_property
from typing import Any
from urllib.parse import urlparse

import httpx
from aiogram import Router
from anthropic import AsyncAnthropic
from baski.clients.playwright_client import PlaywrightClient
from baski.clients.scheduler import CloudTasksConfig, CloudTasksScheduler, Scheduler
from baski.env import get_env
from baski.telegram.server import TelegramServer
from fastapi import FastAPI
from google.cloud import tasks_v2
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app import chat
from app.access import AllowlistMiddleware
from app.assistant import Assistant
from app.scheduling import LoggingScheduler, ScheduleRunner, ScheduleStore, SchedulingService, build_fire_route
from app.shared import CoreDeps


class NisseBot(TelegramServer):
    """Personal-assistant Telegram bot. Polling locally, FastAPI webhook on --cloud."""

    def routers(self) -> Iterable[Router]:
        """Mount the chat router and bind async-client lifecycle to its startup/shutdown."""
        router = chat.build_router(assistant=self.assistant)
        router.startup.register(self._on_startup)
        router.shutdown.register(self._on_shutdown)
        return [router]

    def outer_middlewares(self) -> Iterable[Any]:
        """Gate every message through the owner allow-list before any handler runs."""
        return [AllowlistMiddleware()]

    def cloud_tasks_config(self) -> CloudTasksConfig:
        """Cloud Tasks settings for the inbound-update queue; tasks are OIDC-signed as the cloud-run SA."""
        project = str(get_env("GOOGLE_CLOUD_PROJECT"))
        return CloudTasksConfig(
            client=tasks_v2.CloudTasksAsyncClient(),
            project_id=project,
            location=str(get_env("GOOGLE_CLOUD_REGION")),
            queue=str(get_env("CLOUD_TASKS_QUEUE")),
            invoker_sa_email=f"cloud-run@{project}.iam.gserviceaccount.com",
        )

    def add_webhook_routes(self, app: FastAPI) -> None:
        """Mount the scheduling fire endpoint Cloud Tasks calls when a task is due (webhook mode)."""
        service = SchedulingService(scheduler=self.deps.scheduler, endpoint=self.deps.schedule_endpoint)
        runner = ScheduleRunner(assistant=self.assistant, bot=self.bot, database=self._database, scheduling=service)
        build_fire_route(app, runner)

    @cached_property
    def assistant(self) -> Assistant:
        """The bot's Assistant, built from shared deps."""
        return Assistant(deps=self.deps)

    @cached_property
    def deps(self) -> CoreDeps:
        """Shared low-level clients the conversation assembles every tool from.

        Cloud Tasks `scheduler` + `schedule_endpoint` are wired only in webhook mode; in polling
        there's no public fire callback, so both stay None and scheduling tools aren't built.
        """
        scheduler: Scheduler = LoggingScheduler(self.logger)  # polling/probe: log instead of enqueue
        schedule_endpoint = "http://localhost/schedule/fire"  # unused by LoggingScheduler
        if self.args["cloud"]:
            scheduler = CloudTasksScheduler(self.cloud_tasks_config())
            base = urlparse(self.args["webhook_url"])
            schedule_endpoint = f"{base.scheme}://{base.netloc}/schedule/fire"
        return CoreDeps(
            logger=self.logger,
            http=httpx.AsyncClient(timeout=httpx.Timeout(timeout=30.0)),
            anthropic=AsyncAnthropic(api_key=str(get_env("ANTHROPIC_API_KEY")), timeout=600.0),
            database=self._database,
            playwright=PlaywrightClient(headless=True, logger=self.logger),
            bucket_name=str(get_env("PRIVATE_BUCKET_NAME")),
            scheduler=scheduler,
            schedule_endpoint=schedule_endpoint,
        )

    @cached_property
    def _database(self) -> AsyncDatabase:
        """Default MongoDB database resolved from the connection URI."""
        return AsyncMongoClient(str(get_env("MONGODB_URI")), tz_aware=True).get_default_database()

    @cached_property
    def _resources(self) -> AsyncExitStack:
        """Holds the async clients opened on startup and closed on shutdown."""
        return AsyncExitStack()

    async def _on_startup(self) -> None:
        """Open the HTTP client and headless browser, and ensure memory + schedule indexes."""
        await self._resources.enter_async_context(self.deps.http)
        await self._resources.enter_async_context(self.deps.playwright)
        await self.assistant.setup()
        await ScheduleStore.ensure_indexes(self._database)

    async def _on_shutdown(self) -> None:
        """Close every async client opened on startup."""
        await self._resources.aclose()


if __name__ == "__main__":
    # aiogram's per-update "Update id=… is handled" INFO line bypasses our structured
    # logger and floods the log; keep only its warnings and above.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    sys.exit(NisseBot().run())
