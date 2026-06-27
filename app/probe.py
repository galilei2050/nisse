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
from typing import TYPE_CHECKING

import httpx
from anthropic import AsyncAnthropic
from baski.agents.trace import TraceRecord
from baski.clients.playwright_client import PlaywrightClient
from baski.env import get_env
from baski.server import Logger
from pymongo import AsyncMongoClient

from app.assistant import Assistant
from app.browser import managed_browser_cdp_url
from app.scheduling import LoggingScheduler
from app.shared import CoreDeps
from app.tracing import print_trace

if TYPE_CHECKING:
    from pymongo.asynchronous.database import AsyncDatabase

_TRACES_DIR = Path(__file__).resolve().parent.parent / "scratch" / "traces"  # persisted for `app.trace`


async def _run(user_id: int, message: str) -> None:
    logger = Logger()
    _TRACES_DIR.mkdir(parents=True, exist_ok=True)
    async with AsyncExitStack() as resources:
        http = await resources.enter_async_context(httpx.AsyncClient(timeout=httpx.Timeout(timeout=30.0)))
        cdp_url = managed_browser_cdp_url(logger)
        playwright = await resources.enter_async_context(
            PlaywrightClient(headless=True, logger=logger, cdp_url=cdp_url)
        )
        database: AsyncDatabase = AsyncMongoClient(str(get_env("MONGODB_URI")), tz_aware=True).get_default_database()
        deps = CoreDeps(
            logger=logger,
            http=http,
            anthropic=AsyncAnthropic(api_key=str(get_env("ANTHROPIC_API_KEY")), timeout=600.0),
            database=database,
            playwright=playwright,
            bucket_name=str(get_env("PRIVATE_BUCKET_NAME")),
            scheduler=LoggingScheduler(logger),  # probe has no Cloud Tasks — log the enqueue instead
            schedule_endpoint="http://localhost/schedule/fire",
            browser_cdp_url=cdp_url,
        )
        assistant = Assistant(deps=deps, await_trace=True, local_traces_dir=str(_TRACES_DIR))
        await assistant.setup()
        result = await assistant.run(conversation_id=user_id, text=message)
        await assistant.flush(conversation_id=user_id)  # persist turn writes + soft-deletes, as prod does post-send

    trace = TraceRecord.model_validate_json((_TRACES_DIR / f"{result.trace_id}.json").read_text())
    print_trace(trace)  # compact by default; re-inspect richly with `uv run python -m app.trace`
    print(f"\n=== TRACE SAVED ===\n{result.trace_id}")
    print(f"inspect: uv run python -m app.tracing {result.trace_id} --results [--grep TEXT] [--system] [--full]")


def main() -> None:
    """Parse CLI args and run one probe; the trace is saved under scratch/traces/ for `app.trace`."""
    parser = argparse.ArgumentParser(description="Drive Assistant.run() once for manual end-to-end testing.")
    parser.add_argument("--user-id", type=int, default=1, help="Conversation id (acts as the owner's chat id)")
    parser.add_argument("--message", required=True, help="Text to send to the agent")
    args, _ = parser.parse_known_args()
    asyncio.run(_run(args.user_id, args.message))


if __name__ == "__main__":
    main()
