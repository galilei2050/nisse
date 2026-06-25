"""Agent tools for acting in the chat's logged-in browser session (read via a11y tree, click, type).

Distinct from `browse_website` (baski), which fetches a public page to markdown and is the right tool
for reading an article. These act inside the owner's saved session — open a page behind a login, read
its accessibility tree, click, and fill fields — so use them for *doing* something on a site (account
pages, checkout, forms). Prefer the search tools for finding facts/URLs; reach for the browser only
when you must read behind a login or interact with the page.
"""

from baski.agents.tool import Tool
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel, Field

from app.browser.session import BrowserSession

_ROLE_FIELD = Field(
    description="ARIA role of the target, as shown in the snapshot — e.g. button, link, textbox, combobox, checkbox"
)
_NAME_FIELD = Field(description="Accessible name of the target exactly as the snapshot shows it (the quoted label)")


class WebOpenTool(Tool):
    """Open a URL in the chat's logged-in browser and return the page as an accessibility tree."""

    name = "web_open"
    one_line = "Open a URL in your logged-in browser session; returns the page as an accessibility tree"
    description = (
        "Navigate the logged-in browser to a URL and return the page's accessibility tree (roles + "
        "names). Use for pages behind a login, or any page you intend to act on; for reading a public "
        "article use browse_website instead. The session carries the owner's logins saved via "
        "`make startbrowser`."
    )

    class Input(BaseModel):
        """Arguments for opening a page."""

        url: str = Field(description="Full URL to open, e.g. https://www.doordash.com")

    def __init__(self, session: BrowserSession) -> None:
        """Hold the chat's browser session."""
        self._session = session

    async def execute(self, url: str) -> str:
        """Open the URL and return the page's accessibility tree."""
        try:
            return await self._session.open(url)
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            return f"Could not open {url}: {exc}"


class WebSnapshotTool(Tool):
    """Re-read the current page's accessibility tree (after it changed, or to find an element)."""

    name = "web_snapshot"
    one_line = "Re-read the current page in your browser session as an accessibility tree"
    description = (
        "Return the accessibility tree (roles + names) of the page currently open in the logged-in "
        "browser. Use to see the result of an action or to find the exact role + name of an element "
        "before clicking or typing."
    )

    class Input(BaseModel):
        """No arguments — snapshots the current page."""

    def __init__(self, session: BrowserSession) -> None:
        """Hold the chat's browser session."""
        self._session = session

    async def execute(self) -> str:
        """Return the current page's accessibility tree."""
        try:
            return await self._session.snapshot()
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            return f"Could not read the page: {exc}. Open a page with web_open first."


class WebClickTool(Tool):
    """Click an element in the logged-in browser by its role + accessible name."""

    name = "web_click"
    one_line = "Click an element in your browser session by role + accessible name"
    description = (
        "Click the element with the given ARIA role and accessible name (as shown in the snapshot), "
        "then return the updated accessibility tree. If nothing matches, the page is returned so you "
        "can pick the right role + name."
    )

    class Input(BaseModel):
        """Arguments for a click."""

        role: str = _ROLE_FIELD
        name: str = _NAME_FIELD

    def __init__(self, session: BrowserSession) -> None:
        """Hold the chat's browser session."""
        self._session = session

    async def execute(self, role: str, name: str) -> str:
        """Click the element and return the updated tree, or a recoverable error."""
        try:
            return await self._session.click(role=role, name=name)
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            return f"Could not click {role} '{name}': {exc}. Call web_snapshot to see the current page."


class WebTypeTool(Tool):
    """Type text into a field in the logged-in browser by its role + accessible name."""

    name = "web_type"
    one_line = "Type into a field in your browser session by role + accessible name (optionally submit)"
    description = (
        "Type text into the field with the given ARIA role and accessible name, then return the "
        "updated accessibility tree. Set submit=true to press Enter after (e.g. to run a search). If "
        "nothing matches, the page is returned so you can pick the right field."
    )

    class Input(BaseModel):
        """Arguments for typing into a field."""

        role: str = _ROLE_FIELD
        name: str = _NAME_FIELD
        text: str = Field(description="Text to type into the field")
        submit: bool = Field(default=False, description="Press Enter after typing (submit the form/search)")

    def __init__(self, session: BrowserSession) -> None:
        """Hold the chat's browser session."""
        self._session = session

    async def execute(self, role: str, name: str, text: str, *, submit: bool = False) -> str:
        """Type into the field and return the updated tree, or a recoverable error."""
        try:
            return await self._session.type(role=role, name=name, text=text, submit=submit)
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            return f"Could not type into {role} '{name}': {exc}. Call web_snapshot to see the current page."
