"""make startbrowser U=<chat-id> — open a real browser, log in once, save the session for that chat.

Opens a headed Chromium window using the assistant's own browser config (so the saved session's
fingerprint matches when the assistant replays it). Log into the services the assistant should act
on — and save your payment card on those sites. Press Enter in this terminal when done; the session
(cookies + localStorage) is written to that chat's storage-state file and the assistant reuses it.
Re-run any time a login expires. See docs/browser-actions.md.

The chat-id is the Telegram chat id of the conversation the session belongs to (one cookie jar per
chat). Find it in the backend logs or a trace.
"""

import argparse
import asyncio
from pathlib import Path

import anyio
from baski.clients.playwright_client import PlaywrightClient

from app.shared import browser_state_path

_START_URL = "https://www.google.com"
_PROMPT = "\nLog into your services in the browser window, then press Enter here to save the session... "


async def _run(path: Path) -> None:
    async with PlaywrightClient(headless=False, storage_state=str(path)) as client:
        page = await client.new_page()
        await page.goto(_START_URL)
        await anyio.to_thread.run_sync(input, _PROMPT)
        await client.save_storage_state(str(path))
    print(f"Session saved to {path}")


def main() -> None:
    """Resolve the chat's session path, ensure its dir exists, then run the headed login flow."""
    parser = argparse.ArgumentParser(description="Open a browser to log in once; save the session for a chat.")
    parser.add_argument("--chat-id", type=int, required=True, help="Telegram chat id the session belongs to")
    args = parser.parse_args()

    path = browser_state_path(args.chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(_run(path))


if __name__ == "__main__":
    main()
