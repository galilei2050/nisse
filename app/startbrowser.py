"""make startbrowser U=<chat-id> — open a real browser to log in once; save the session for the agent.

Launches the Chromium binary directly (NOT via Playwright's launch, which sets automation flags that
Cloudflare/Turnstile detect and block — that's why bot-protected sites like DoorDash refuse the
agent's own browser until it has a valid session). A persistent on-disk profile is reused across runs,
so logins you've already done stick; you just open whatever sites you need, log in, and on exit the
updated session (cookies + localStorage) is saved to this chat's MongoDB document. The agent then
loads it and — carrying the real session, including the Cloudflare clearance cookie — gets through
automated and headless. Mongo (not local disk) is also what makes the session reach production, since
Cloud Run is stateless. See docs/browser-actions.md.

Run: `make startbrowser U=<chat-id>` (chat-id = the Telegram chat the session belongs to).
"""

import argparse
import asyncio
import subprocess
import time
from pathlib import Path

from baski.env import get_env
from playwright.sync_api import StorageState, sync_playwright
from pymongo import AsyncMongoClient

from app.browser import BrowserSessionStore

_PORT = 9222
_START_URL = "https://www.doordash.com"
# local-only, git-ignored: the interactive login profile (kept inside the repo, never under $HOME)
_PROFILE_ROOT = Path(__file__).resolve().parent.parent / "scratch" / "chrome-profiles"


def _chromium_binary() -> str:
    """Path to Playwright's Chromium binary — launched directly so it carries no automation flags."""
    with sync_playwright() as pw:
        return pw.chromium.executable_path


def _export_session() -> StorageState:  # noqa: ANON002 — StorageState is a Playwright TypedDict
    """Attach to the running Chrome over CDP and read its session (cookies + localStorage)."""
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://localhost:{_PORT}")
        state = browser.contexts[0].storage_state()
        browser.close()  # detaches CDP; the real Chrome is closed separately
        return state


async def _save_session(chat_id: int, state: StorageState) -> None:  # noqa: ANON002 — Playwright TypedDict
    """Write the captured session to this chat's MongoDB document — what the agent reads."""
    client: AsyncMongoClient = AsyncMongoClient(str(get_env("MONGODB_URI")), tz_aware=True)
    try:
        store = BrowserSessionStore(client.get_default_database(), conversation_id=chat_id)
        await store.ensure_indexes(client.get_default_database())
        await store.save(state)
    finally:
        await client.close()


def main() -> None:
    """Launch real Chrome on a persistent profile, wait for the owner to log in, save the session."""
    parser = argparse.ArgumentParser(description="Log in once in a real browser; save the session for a chat.")
    parser.add_argument("--chat-id", type=int, required=True, help="Telegram chat id the session belongs to")
    chat_id = parser.parse_args().chat_id

    profile = _PROFILE_ROOT / f"chrome-profile-{chat_id}"  # persistent → prior logins are reused next run
    profile.mkdir(parents=True, exist_ok=True)

    chrome = subprocess.Popen(  # noqa: S603 — Playwright's own Chromium binary, no shell
        [
            _chromium_binary(),
            f"--remote-debugging-port={_PORT}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            _START_URL,
        ]
    )
    try:
        time.sleep(3)  # let Chrome bring up the CDP endpoint
        input("\nLog into the sites the agent should act on, then press Enter to save the session... ")
        state = _export_session()
        asyncio.run(_save_session(chat_id, state))
        print(f"Saved {len(state.get('cookies', []))} cookies for chat {chat_id} to MongoDB")
    finally:
        chrome.terminate()


if __name__ == "__main__":
    main()
