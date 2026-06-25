"""Per-chat logged-in browser session for *acting* on web pages, not just reading them.

One isolated browser context per conversation, loaded with that chat's saved session (`make
startbrowser`), branched off the shared browser. Pages are read as an accessibility tree — roles +
names, compact text, no screenshots (cheaper tokens and more stable than pixels). Elements are acted
on by the same role+name the snapshot exposes, e.g. `get_by_role("button", name="Add to cart")`.

Single-owner scope: the context + page are opened lazily on first action and live for the bot's
lifetime (closed when the shared browser closes). Fine for one owner; revisit if many chats act.
"""

import contextlib
from typing import Any, cast

from baski.clients.playwright_client import PlaywrightClient
from playwright.async_api import BrowserContext, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.browser.proxy import ProxyPool

_ACTION_TIMEOUT = 15000  # ms — fail an action fast so the agent can re-read and retry, not hang 90s
_SETTLE_TIMEOUT = 4000  # ms — bounded wait for async content to render before snapshotting


class BrowserSession:
    """One chat's live, logged-in page. Read it with `snapshot`; act with `open`/`click`/`type`."""

    def __init__(self, *, client: PlaywrightClient, storage_state: str, proxy_pool: ProxyPool | None = None) -> None:
        """Hold the shared browser, this chat's storage-state path, and the proxy pool; context opens lazily."""
        self._client = client
        self._storage_state = storage_state
        self._proxy_pool = proxy_pool
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def _ensure_context(self, url: str | None) -> BrowserContext:
        """Open the chat's context on first use, routing it through the proxy pinned to `url`'s host."""
        if self._context is not None:
            return self._context
        proxy = self._proxy_pool.for_url(url) if (self._proxy_pool and url) else None
        self._context = await self._client.new_context(self._storage_state, proxy=proxy.model_dump() if proxy else None)
        return self._context

    async def _live_page(self, url: str | None = None) -> Page:
        """The chat's persistent page, opening its (proxied, logged-in) context on first use."""
        context = await self._ensure_context(url)
        if self._page is None or self._page.is_closed():
            self._page = await context.new_page()
        return self._page

    async def open(self, url: str) -> str:
        """Navigate to a URL and return the page's accessibility tree."""
        page = await self._live_page(url)
        await page.goto(url, wait_until="domcontentloaded")
        return await _settled_snapshot(page)

    async def snapshot(self) -> str:
        """Re-read the current page's accessibility tree."""
        return await _snapshot(await self._live_page())

    async def click(self, *, role: str, name: str) -> str:
        """Click the element with this role + accessible name, then return the updated tree."""
        page = await self._live_page()
        await page.get_by_role(cast("Any", role), name=name).click(timeout=_ACTION_TIMEOUT)
        return await _settled_snapshot(page)

    async def type(self, *, role: str, name: str, text: str, submit: bool) -> str:
        """Type text into the field with this role + name; press Enter when `submit`. Return the tree."""
        page = await self._live_page()
        field = page.get_by_role(cast("Any", role), name=name)
        await field.fill(text, timeout=_ACTION_TIMEOUT)
        if submit:
            await field.press("Enter")
        return await _settled_snapshot(page)


async def _settled_snapshot(page: Page) -> str:
    """Let async (XHR/SPA) content render after an action, then snapshot — many sites load results late.

    Bounded wait: if the network never goes idle (sockets/polling), give up and snapshot what's there.
    """
    with contextlib.suppress(PlaywrightTimeoutError):
        await page.wait_for_load_state("networkidle", timeout=_SETTLE_TIMEOUT)
    return await _snapshot(page)


async def _snapshot(page: Page) -> str:
    """Accessibility tree (roles + names) of the page body — the agent's compact view of the DOM."""
    return await page.locator("body").aria_snapshot()
