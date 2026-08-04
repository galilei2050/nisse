"""Manual curator run — one maintenance pass, outside Cloud Scheduler, printing what it did.

The nightly pass is the hardest thing in this codebase to observe: it runs while the owner sleeps
and its product is a set of edits spread across four stores. This driver runs the real pass against
the real database and then prints the three things worth checking:

  1. EVIDENCE — the digest and the classification the curator was given.
  2. CHANGES — every revision it wrote, with the text it replaced.
  3. REPORT — what it would have told the owner.

    make curate U=<conversation_id>              # act: the pass edits the stores for real
    make curate U=<conversation_id> DAYS=7       # review a wider window

Real API and real Mongo, like `app/probe.py`. It edits live stores — the changes are attributed and
recoverable from the revision log, but use a throwaway conversation id when experimenting.
"""

import argparse
import asyncio
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, cast

import httpx
from anthropic import AsyncAnthropic
from baski.agents import GeminiJudge
from baski.clients.playwright_client import PlaywrightClient
from baski.env import get_env
from baski.primitives import datetime
from baski.server.logger import configure_logging
from pymongo import AsyncMongoClient

from app.assistant import NISSE_JUDGE_PROMPT
from app.curator.classify import classify
from app.curator.curator import Curator
from app.curator.evidence import collect, render
from app.scheduling import LoggingScheduler
from app.shared import CoreDeps
from app.shared.revisions import RevisionLog
from app.tools.wiring import build_tool_registry

if TYPE_CHECKING:
    from aiogram import Bot
    from pymongo.asynchronous.database import AsyncDatabase


class _PrintingBot:
    """Stands in for the Bot: the report goes to stdout instead of Telegram."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, *, chat_id: int, text: str) -> None:  # noqa: ARG002 — matches Bot.send_message
        """Capture the report the owner would have received."""
        self.sent.append(text)


async def _run(conversation_id: int, days: int, *, dry_run: bool) -> None:
    async with AsyncExitStack() as resources:
        http = await resources.enter_async_context(httpx.AsyncClient(timeout=httpx.Timeout(timeout=30.0)))
        database: AsyncDatabase = AsyncMongoClient(str(get_env("MONGODB_URI")), tz_aware=True).get_default_database()
        anthropic = AsyncAnthropic(api_key=str(get_env("ANTHROPIC_API_KEY")), timeout=600.0)
        window = datetime.timedelta(days=days)

        evidence = await collect(database, conversation_id=conversation_id, since=datetime.now() - window)
        print(f"\n=== EVIDENCE — {len(evidence.exchanges)} exchanges, {evidence.reaction_count} reacted ===")
        print(render(evidence))
        if not evidence.exchanges:
            print("\nNothing in this window — the pass would record an idle run and stop.")
            return

        classification = await classify(anthropic, evidence)
        print("\n=== CLASSIFICATION ===")
        for signal in classification.signals:
            print(f"turn {signal.turn_id} · {signal.kind} · {signal.about} · “{signal.quote}”")
        if dry_run:
            print("\n(dry run — stopping before the curator edits anything)")
            return

        bot = _PrintingBot()
        playwright = await resources.enter_async_context(PlaywrightClient(headless=True))
        deps = CoreDeps(
            http=http,
            anthropic=anthropic,
            database=database,
            playwright=playwright,  # unused by the curator's tools, but CoreDeps is one shape for every caller
            bucket_name=str(get_env("PRIVATE_BUCKET_NAME")),
            scheduler=LoggingScheduler(),
            schedule_endpoint="http://localhost/schedule/fire",
            judge=GeminiJudge(project=str(get_env("GOOGLE_CLOUD_PROJECT")), instructions=NISSE_JUDGE_PROMPT),
            tools=build_tool_registry(),
            bot=cast("Bot", bot),
        )
        run = await Curator(deps, bot=cast("Bot", bot)).curate(conversation_id=conversation_id, window=window)

        changes = await RevisionLog(database, conversation_id=conversation_id).for_run(run.run_id)
        print(f"\n=== CHANGES ({len(changes)}) ===")
        for change in changes:
            print(f"\n[{change.collection}/{change.target}] {change.kind} by {change.actor}")
            print(f"  before: {(change.before or '—')[:300]}")
            print(f"  after:  {(change.after or '—')[:300]}")

        print(f"\n=== REPORT (run {run.run_id}, ${run.cost:.4f}) ===\n{run.report}")


def main() -> None:
    """Parse CLI args and run one curator pass."""
    parser = argparse.ArgumentParser(description="Run one curator maintenance pass end-to-end.")
    parser.add_argument("--conversation-id", type=int, required=True, help="Chat to curate")
    parser.add_argument("--days", type=int, default=1, help="How far back to review")
    parser.add_argument("--dry-run", action="store_true", help="Stop after the classification; change nothing")
    args, _ = parser.parse_known_args()
    configure_logging(cloud=False, debug=False)
    asyncio.run(_run(args.conversation_id, args.days, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
