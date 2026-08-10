"""Per-chat logged-in browser session for *acting* on web pages, not just reading them.

One isolated browser context per conversation, loaded with that chat's saved session (`make
startbrowser`), branched off the shared browser. Pages are read as an *indexed element listing*: each
visible interactive element (button/link/field) is tagged with a numeric `ref` and shown with its
nearby text — including prices and product names that the bare accessibility name omits. The agent
acts by `ref`, which sidesteps the two ways `aria_snapshot` role+name breaks on real apps: identical
button names (every "Add" button looks the same) and prices living in non-ARIA text. This is the
browser-use / Stagehand pattern (deterministic DOM extraction → compact action list). Lazy-loaded
grids need `scroll` to render — a first-class action — after which a fresh snapshot re-tags refs.

Single-owner scope: the context + page are opened lazily on first action and live for the bot's
lifetime (closed when the shared browser closes). Fine for one owner; revisit if many chats act.

`ref`s are valid only for the most recent snapshot — every action returns a freshly-tagged listing,
so always act on refs from the latest output.
"""

import contextlib

from baski.clients.playwright_client import PlaywrightClient
from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError

from app.browser.proxy import ProxyPool
from app.browser.store import BrowserSessionStore

_ACTION_TIMEOUT = 15000  # ms — fail an action fast so the agent can re-read and retry, not hang 90s
_SETTLE_QUIET = 500  # ms of no DOM mutations that counts as "rendered"
_SETTLE_CAP = 2500  # ms hard cap — don't wait forever on a page that never goes quiet


class BrowserSession:
    """One chat's live, logged-in page. Read it with `snapshot`; act by `ref` with `click`/`type`/`scroll`."""

    def __init__(
        self, *, client: PlaywrightClient, session_store: BrowserSessionStore, proxy_pool: ProxyPool | None = None
    ) -> None:
        """Hold the shared browser, this chat's session store, and the proxy pool; context opens lazily."""
        self._client = client
        self._session_store = session_store
        self._proxy_pool = proxy_pool
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def _ensure_context(self, url: str | None) -> BrowserContext:
        """Open the chat's context on first use, loading its saved session and pinning `url`'s proxy."""
        if self._context is not None:
            return self._context
        storage_state = await self._session_store.load()
        proxy = self._proxy_pool.for_url(url) if (self._proxy_pool and url) else None
        self._context = await self._client.new_context(storage_state, proxy=proxy.model_dump() if proxy else None)
        return self._context

    async def _live_page(self, url: str | None = None) -> Page:
        """The chat's persistent page, opening its (proxied, logged-in) context on first use."""
        context = await self._ensure_context(url)
        if self._page is None or self._page.is_closed():
            self._page = await context.new_page()
        return self._page

    async def open(self, url: str) -> str:
        """Navigate to a URL and return the indexed element listing."""
        page = await self._live_page(url)
        await page.goto(url, wait_until="domcontentloaded")
        return await _settled_snapshot(page)

    async def snapshot(self) -> str:
        """Re-read the current page as a fresh indexed element listing (re-tags refs)."""
        return await _snapshot(await self._live_page())

    async def click(self, *, ref: int) -> str:
        """Click the element with this ref, then return the updated listing.

        `force=True` dispatches the click straight to the element, skipping the "receives pointer
        events" actionability check. DoorDash (and similar) layer an invisible Cloudflare Turnstile
        div over the page that intercepts normal clicks; force-click lands on the real button instead.
        Safe here because the listing only contains visible elements (the index JS filters by visibility).
        """
        page = await self._live_page()
        await page.locator(f"[data-nisse-ref='{ref}']").click(timeout=_ACTION_TIMEOUT, force=True)
        return await _settled_snapshot(page)

    async def type(self, *, ref: int, text: str, submit: bool) -> str:
        """Type text into the field with this ref; press Enter when `submit`. Return the listing."""
        page = await self._live_page()
        field = page.locator(f"[data-nisse-ref='{ref}']")
        await field.fill(text, timeout=_ACTION_TIMEOUT)
        if submit:
            await field.press("Enter", timeout=_ACTION_TIMEOUT)
        return await _settled_snapshot(page)

    async def scroll(self) -> str:
        """Scroll down to render lazy-loaded content (product grids), then return the fresh listing."""
        page = await self._live_page()
        await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
        return await _settled_snapshot(page)


_DOM_QUIET_JS = """
({quiet, cap}) => new Promise((resolve) => {
  const finish = () => { obs.disconnect(); clearTimeout(quietTimer); clearTimeout(capTimer); resolve(); };
  let quietTimer = setTimeout(finish, quiet);
  const capTimer = setTimeout(finish, cap);
  const obs = new MutationObserver(() => {
    clearTimeout(quietTimer);
    quietTimer = setTimeout(finish, quiet);
  });
  obs.observe(document.documentElement, {childList: true, subtree: true, attributes: true, characterData: true});
})
"""

# Tag every visible interactive element with a numeric data-nisse-ref and return an indexed listing,
# each line merging the element's own label with the nearest ancestor's text (the product card) — so
# prices and names that the accessibility tree drops are surfaced for the agent to read and compare.
_INDEX_JS = r"""
() => {
  const SEL = 'a,button,input,textarea,select,[role=button],[role=link],[role=textbox],[contenteditable=true]';
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const label = (el) => (el.getAttribute('aria-label') || el.getAttribute('alt') || el.getAttribute('placeholder')
    || el.getAttribute('title') || el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 100);
  const context = (el) => { let n = el, best = '';
    for (let i = 0; i < 4 && n; i++) { n = n.parentElement; if (!n) break;
      const t = (n.innerText || '').trim().replace(/\s+/g, ' '); if (t.length > best.length) best = t;
      if (best.length > 180) break; }
    return best.slice(0, 180); };
  const lines = [];
  // Drop the previous pass's refs first. An element that scrolled out or was hidden keeps the number
  // it was given, and `page.locator` is strict: a re-used number then resolves to two elements and
  // the click fails on a ref this listing just printed — with re-snapshotting unable to clear it.
  document.querySelectorAll('[data-nisse-ref]').forEach((el) => el.removeAttribute('data-nisse-ref'));
  [...document.querySelectorAll(SEL)].filter(vis).forEach((el, i) => {
    el.setAttribute('data-nisse-ref', i);
    const role = el.getAttribute('role') || el.tagName.toLowerCase();
    const lab = label(el), ctx = context(el);
    lines.push(`[${i}] ${role} "${lab}"` + (ctx && ctx !== lab ? ` — ${ctx}` : ''));
  });
  return lines.join('\n');
}
"""


_CF_TITLE_MARKERS = ("just a moment", "verifying", "security verification", "attention required")
_CF_WAIT_MS = 3000  # poll interval while a managed challenge auto-solves
_CF_TRIES = 6  # ~18s total — a cleared session's managed challenge solves in 5-20s (else it's a hard block)


async def _settled_snapshot(page: Page) -> str:
    """Let async (XHR/SPA) content render after an action, then snapshot — many sites load results late.

    Wait for the DOM to stop mutating, not for the network to go idle: telemetry/websockets keep the
    network busy forever on apps like DoorDash, so `networkidle` only ever times out. Resolve after
    `_SETTLE_QUIET` ms of no DOM changes, capped at `_SETTLE_CAP`. Suppress errors from a navigation
    that destroys the execution context mid-wait — just snapshot what's there.

    Then, if the page is a Cloudflare interstitial ("Just a moment…"), wait for it to auto-clear rather
    than returning the challenge: a session carrying a valid clearance cookie solves the managed
    challenge in a few seconds, but the quick settle above can catch it mid-solve. Polls up to ~18s; if
    it never clears, the browser is being hard-blocked and the challenge text is returned as-is.
    """
    with contextlib.suppress(PlaywrightError):
        await page.evaluate(_DOM_QUIET_JS, {"quiet": _SETTLE_QUIET, "cap": _SETTLE_CAP})
    for _ in range(_CF_TRIES):
        if not await _is_cf_challenge(page):
            break
        with contextlib.suppress(PlaywrightError):
            await page.wait_for_timeout(_CF_WAIT_MS)
    return await _snapshot(page)


async def _is_cf_challenge(page: Page) -> bool:
    """True while the page is a Cloudflare interstitial (detected by its title)."""
    with contextlib.suppress(PlaywrightError):
        title = (await page.title()).lower()
        return any(marker in title for marker in _CF_TITLE_MARKERS)
    return False


_TEXT_CAP = 6000  # chars of visible page text appended to the listing


async def _snapshot(page: Page) -> str:
    """The page as the agent sees it: indexed interactive elements (act by [ref]) + visible page text.

    The interactive listing alone is blind to non-interactive text — order totals, delivery-slot
    labels, confirmation messages ("Order Placed") — which the agent needs to read a total before
    paying and to verify an order actually went through. So append the page's visible text (capped).
    """
    title = await page.title()
    listing = await page.evaluate(_INDEX_JS)
    count = len(listing.splitlines())
    body = listing or "(no interactive elements found — try scroll, or the page may still be loading)"
    text = (await page.locator("body").inner_text()).strip()
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())[:_TEXT_CAP]
    return (
        f"# {title} — {count} interactive elements (act by [ref]; scroll to load more)\n{body}\n\n"
        f"--- VISIBLE PAGE TEXT (read totals / status here; not clickable) ---\n{text}"
    )
