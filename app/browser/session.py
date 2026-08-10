"""Per-chat browser session for *acting* on web pages, not just reading them.

One isolated browser context per conversation, branched off the shared browser and carrying whatever
session the chat has saved — today none, since nothing writes `browser_sessions`.

Pages are read as an *indexed element listing*: each
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

import asyncio
import contextlib

from baski.clients.playwright_client import PlaywrightClient
from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError

from app.browser.proxy import ProxyPool
from app.browser.store import BrowserSessionStore


class NoPageOpenError(RuntimeError):
    """An action was asked for before any page was opened.

    Carries its own agent-facing text and is deliberately NOT caught by the tools: baski's loop logs the
    traceback, marks the result `is_error=True` and hands the model this message. Catching it to return a
    sentence would spend the loudness and buy nothing — the model reads the same words either way, while
    the trace would record a failed action as a successful call.
    """

    def __init__(self) -> None:
        """One phrasing, at the layer that knows the condition."""
        super().__init__("No page is open in this session. Call web_open with a URL first.")


def _usable(context: BrowserContext) -> bool:
    """Whether this context's browser is still there — a dead one fails every action, forever."""
    browser = context.browser
    return browser is not None and browser.is_connected()


_ACTION_TIMEOUT = 15000  # ms — fail an action fast so the agent can re-read and retry, not hang 90s
_SETTLE_QUIET = 500  # ms of no DOM mutations that counts as "rendered"
_SETTLE_CAP = 2500  # ms hard cap — don't wait forever on a page that never goes quiet


class BrowserSession:
    """One chat's live page. Read it with `snapshot`; act by `ref` with `click`/`type`/`scroll`."""

    def __init__(
        self, *, client: PlaywrightClient, session_store: BrowserSessionStore, proxy_pool: ProxyPool | None = None
    ) -> None:
        """Hold the shared browser, this chat's session store, and the proxy pool; context opens lazily."""
        self._client = client
        self._session_store = session_store
        self._proxy_pool = proxy_pool
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._opening = asyncio.Lock()

    async def _prepare_page(self, url: str) -> Page:
        """This chat's page, building the context and the page on first use. Caller holds `_opening`.

        A context whose browser has gone (Cloud Run OOM-kills Chromium) is dropped rather than reused: the
        cached one fails every action for the rest of the process, and each failure would be phrased as if
        a retry could help.
        """
        if self._context is not None and not _usable(self._context):
            self._context, self._page = None, None
        if self._context is None:
            storage_state = await self._session_store.load()
            proxy = self._proxy_pool.for_url(url) if self._proxy_pool else None
            self._context = await self._client.new_context(storage_state, proxy=proxy.model_dump() if proxy else None)
        if self._page is None or self._page.is_closed():
            self._page = await self._context.new_page()
        return self._page

    def _require_open_page(self) -> Page:
        """The page a previous `open` left, or a refusal — never a blank one built to satisfy the call.

        Acting methods must not reach the lazy open: it would create `about:blank`, and they then answered
        "0 interactive elements", which reads as "the page is empty or still loading" — measured on a
        first-call `web_snapshot`, and the worker reported exactly that to the owner.
        """
        if self._page is None or self._page.is_closed():
            raise NoPageOpenError
        return self._page

    async def open(self, url: str) -> str:
        """Navigate to a URL and return the indexed element listing.

        Held under `_opening` for the whole navigate-and-read, not just the lazy build. A turn's tool calls
        run concurrently (baski gathers them, parallel tool use enabled) and there is ONE page per chat, so
        two `web_open`s in one turn otherwise interleave their `goto` and their snapshot on that page and
        each can return the other's content. Measured before this was serialized: both callers navigated,
        `self._page` kept one of them, and the next click resolved its ref against the other site while the
        model read its own listing for this one. Serial latency for a case that is already a model mistake.
        """
        async with self._opening:
            page = await self._prepare_page(url)
            await page.goto(url, wait_until="domcontentloaded")
            return await _settled_snapshot(page)

    async def snapshot(self) -> str:
        """Re-read the current page as a fresh indexed element listing (re-tags refs)."""
        return await _snapshot(self._require_open_page())

    async def click(self, *, ref: int) -> str:
        """Click the element with this ref, then return the updated listing.

        `force=True` dispatches the click straight to the element, skipping the "receives pointer
        events" actionability check. DoorDash (and similar) layer an invisible Cloudflare Turnstile
        div over the page that intercepts normal clicks; force-click lands on the real button instead.
        Safe here because the listing only contains visible elements (the index JS filters by visibility).
        """
        page = self._require_open_page()
        await page.locator(f"[data-nisse-ref='{ref}']").click(timeout=_ACTION_TIMEOUT, force=True)
        return await _settled_snapshot(page)

    async def type(self, *, ref: int, text: str, submit: bool) -> str:
        """Type text into the field with this ref; press Enter when `submit`. Return the listing."""
        page = self._require_open_page()
        field = page.locator(f"[data-nisse-ref='{ref}']")
        await field.fill(text, timeout=_ACTION_TIMEOUT)
        if submit:
            await field.press("Enter", timeout=_ACTION_TIMEOUT)
        return await _settled_snapshot(page)

    async def scroll(self) -> str:
        """Scroll down to render lazy-loaded content (product grids), then return the fresh listing."""
        page = self._require_open_page()
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
