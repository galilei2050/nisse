"""Agent tools for acting in the chat's logged-in browser session (read as an indexed element list).

Distinct from `browse_website` (baski), which fetches a public page to markdown and is the right tool
for reading an article. These act inside the owner's saved session — open a page behind a login, read
its interactive elements, click, type, scroll — so use them for *doing* something on a site (account
pages, checkout, forms). Each read returns an indexed listing: `[ref] role "label" — nearby text`
(the nearby text carries prices and product names). Act by `ref`; refs are valid only for the most
recent listing. Prefer the search tools for finding facts/URLs; reach for the browser only when you
must act behind a login.
"""

from baski.agents.tool import Tool
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel, Field

from app.browser.session import BrowserSession

_REF_FIELD = Field(description="The [ref] number of the target element, from the most recent listing")


class WebOpenTool(Tool):
    """Open a URL in the chat's logged-in browser and return the page as an indexed element listing."""

    name = "web_open"
    one_line = "Open a URL in your logged-in browser session; returns the page's interactive elements by [ref]"
    description = (
        "Navigate the logged-in browser to a URL and return the page's interactive elements as an "
        'indexed listing — `[ref] role "label" — nearby text` (the nearby text carries prices and '
        "product names). Use for pages behind a login, or any page you intend to act on; for reading a "
        "public article use browse_website. The session carries the owner's logins from `make startbrowser`."
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
            return f"Could not open {url}: {exc}"


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
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            return f"Could not read the page: {exc}. Open a page with web_open first."


class WebClickTool(Tool):
    """Click an element in the logged-in browser by its [ref] from the latest listing."""

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
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            return f"Could not click [{ref}]: {exc}. Call web_snapshot to get current refs."


class WebTypeTool(Tool):
    """Type text into a field in the logged-in browser by its [ref] from the latest listing."""

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

    async def execute(self, ref: int, text: str, *, submit: bool = False) -> str:
        """Type into the field and return the updated listing, or a recoverable error."""
        try:
            return await self._session.type(ref=ref, text=text, submit=submit)
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            return f"Could not type into [{ref}]: {exc}. Call web_snapshot to get current refs."


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
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            return f"Could not scroll: {exc}. Open a page with web_open first."
