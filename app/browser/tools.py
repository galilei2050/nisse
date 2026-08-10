"""Agent tools for ACTING on a web page — click, type, scroll (the page is read as an indexed list).

Distinct from `browse_website` (baski), which fetches a page to markdown: that answers "what does this
page say", these answer "what does it say AFTER I pick dates and press the button". Each read returns
an indexed listing: `[ref] role "label" — nearby text` (the nearby text carries prices and product
names). Act by `ref`; refs are valid only for the most recent listing.

**No saved logins.** `BrowserSessionStore.load` returns None for every chat because nothing writes
`browser_sessions` (the capture flow was not ported — `docs/browser-actions.md`), so a context opens
signed out and a page behind a sign-in returns its login wall as ordinary content. That distinction
cannot be made by this layer, so it is stated where the chooser and the holder both read it: these
tools are for public pages you have to interact with, not for accounts.
"""

import logging

from baski.agents.tool import Tool
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel, Field

from app.browser.session import BrowserSession, NoPageOpenError
from app.browser.store import BrowserSessionStore
from app.shared import CoreDeps
from app.tools.registry import ToolRegistrar

logger = logging.getLogger(__name__)

# The registry name the five actions live under. Declared because it is compared and looked up rather
# than read: the wiring registers it, `tool_names` may name it, and a test asserts it stays off MAIN_TOOLS.
BROWSER_TOOL_NAME = "browser"


# Each action hands the model a sentence instead of raising, so a stale ref or a dead page is something
# it can correct on the next turn rather than a lost turn. The cost is that baski's tool loop never sees
# the exception, so it neither logs it nor marks the result as an error — which is why every one of these
# logs first. Without that a browser failure in production leaves no trace anywhere and the trace records
# the call as a success.
def _failed(action: str, exc: Exception, **fields: object) -> str:
    """Log one failed browser action and return the sentence the model reads."""
    logger.warning("Browser action failed", extra={"action": action, "error": str(exc), **fields})
    return f"{action} failed: {exc}."


_REF_FIELD = Field(description="The [ref] number of the target element, from the most recent listing")


class WebOpenTool(Tool):
    """Open a URL in the chat's browser and return the page as an indexed element listing."""

    name = "web_open"
    one_line = (
        "Open a URL in a real browser you can act in — returns the page's interactive elements by [ref]. "
        "NO saved logins: it opens signed out, so it reaches public pages and forms, not accounts"
    )
    description = (
        "Navigate the browser to a URL and return the page's interactive elements as an "
        'indexed listing — `[ref] role "label" — nearby text` (the nearby text carries prices and '
        "product names). Use it for any page you need to ACT on — pick dates, submit a form, read what "
        "the page says back; for reading a public article use browse_website. There are no saved logins, "
        "so anything behind a sign-in shows you the login wall — read the page, do not assume you are in."
    )

    class Input(BaseModel):
        """Arguments for opening a page."""

        url: str = Field(description="Full URL to open, e.g. https://www.doordash.com")

    def __init__(self, session: BrowserSession) -> None:
        """Hold the chat's browser session."""
        self._session = session

    async def execute(self, url: str) -> str:
        """Open the URL and return the indexed element listing."""
        try:
            return await self._session.open(url)
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            return _failed("web_open", exc, url=url)


class WebSnapshotTool(Tool):
    """Re-read the current page as a fresh indexed element listing (after it changed, or to get refs)."""

    name = "web_snapshot"
    one_line = "Re-read the current page in your browser session as an indexed element listing"
    description = (
        "Return the current page's interactive elements as a fresh indexed listing `[ref] role "
        '"label" — nearby text`. Use to see the result of an action or to get the current [ref] of an '
        "element before clicking or typing (refs change after every action)."
    )

    class Input(BaseModel):
        """No arguments — snapshots the current page."""

    def __init__(self, session: BrowserSession) -> None:
        """Hold the chat's browser session."""
        self._session = session

    async def execute(self) -> str:
        """Return the current page's indexed element listing."""
        try:
            return await self._session.snapshot()
        except NoPageOpenError:
            return "No page is open in this session. Call web_open with a URL first."
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            return _failed("web_snapshot", exc)


class WebClickTool(Tool):
    """Click an element in the chat's browser by its [ref] from the latest listing."""

    name = "web_click"
    one_line = "Click an element in your browser session by its [ref]"
    description = (
        "Click the element with the given [ref] (from the most recent listing), then return the "
        "updated listing. Refs change after every action, so use one from the latest output."
    )

    class Input(BaseModel):
        """Arguments for a click."""

        ref: int = _REF_FIELD

    def __init__(self, session: BrowserSession) -> None:
        """Hold the chat's browser session."""
        self._session = session

    async def execute(self, ref: int) -> str:
        """Click the element and return the updated listing, or a recoverable error."""
        try:
            return await self._session.click(ref=ref)
        except NoPageOpenError:
            return "No page is open in this session. Call web_open with a URL first."
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            return _failed("web_click", exc, ref=ref) + " Call web_snapshot to get current refs."


class WebTypeTool(Tool):
    """Type text into a field in the chat's browser by its [ref] from the latest listing."""

    name = "web_type"
    one_line = "Type into a field in your browser session by its [ref] (optionally submit)"
    description = (
        "Type text into the field with the given [ref], then return the updated listing. Set "
        "submit=true to press Enter after (e.g. to run a search). Refs change after every action."
    )

    class Input(BaseModel):
        """Arguments for typing into a field."""

        ref: int = _REF_FIELD
        text: str = Field(description="Text to type into the field")
        submit: bool = Field(default=False, description="Press Enter after typing (submit the form/search)")

    def __init__(self, session: BrowserSession) -> None:
        """Hold the chat's browser session."""
        self._session = session

    async def execute(self, ref: int, text: str, *, submit: bool) -> str:
        """Type into the field and return the updated listing, or a recoverable error."""
        try:
            return await self._session.type(ref=ref, text=text, submit=submit)
        except NoPageOpenError:
            return "No page is open in this session. Call web_open with a URL first."
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            return _failed("web_type", exc, ref=ref) + " Call web_snapshot to get current refs."


class WebScrollTool(Tool):
    """Scroll the page down to render lazy-loaded content (e.g. product grids), then return the listing."""

    name = "web_scroll"
    one_line = "Scroll down to load more of the page (product grids load on scroll), returns the new listing"
    description = (
        "Scroll the current page down by about two screens to render lazy-loaded content — product "
        "grids and search results often only appear after scrolling — then return the fresh listing. "
        "Call repeatedly to load more items."
    )

    class Input(BaseModel):
        """No arguments — scrolls the current page down."""

    def __init__(self, session: BrowserSession) -> None:
        """Hold the chat's browser session."""
        self._session = session

    async def execute(self) -> str:
        """Scroll down and return the updated listing."""
        try:
            return await self._session.scroll()
        except NoPageOpenError:
            return "No page is open in this session. Call web_open with a URL first."
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            return _failed("web_scroll", exc)


def browser_tools(deps: CoreDeps, conversation_id: int) -> list[Tool]:
    """The five actions over one chat's browser session — one session, so they share a page.

    No proxy pool: `load_proxy_pool` requires `BROWSER_PROXIES`, which nothing sets, and a pool of one
    unconfigured entry would be worse than none. Add it here when residential egress is actually set up.
    """
    session = BrowserSession(
        client=deps.playwright,
        session_store=BrowserSessionStore(deps.database, conversation_id=conversation_id),
    )
    return [
        WebOpenTool(session),
        WebSnapshotTool(session),
        WebClickTool(session),
        WebTypeTool(session),
        WebScrollTool(session),
    ]


def register_tools(registrar: ToolRegistrar) -> None:
    """Register the five actions under one name, so a roster names `browser` and gets the whole set."""
    registrar.register(BROWSER_TOOL_NAME, browser_tools)
