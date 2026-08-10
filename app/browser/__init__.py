"""Logged-in browser actions — a per-chat session the agent reads as an element listing and acts on.

**Not wired.** There is no `register_tools` here, so the shared registry (`app/tools/`) does not know
these names and no agent can call them; a caller that wants them constructs `BrowserSession` itself.
Nothing writes the `browser_sessions` collection either, so `BrowserSessionStore.load` returns None
and every session opens logged out — read the page before believing you are signed in.

Design, the bot-protection findings and what wiring will need: `docs/browser-actions.md`.
"""

from app.browser.proxy import ProxyPool, load_proxy_pool
from app.browser.session import BrowserSession
from app.browser.store import BrowserSessionStore
from app.browser.tools import WebClickTool, WebOpenTool, WebScrollTool, WebSnapshotTool, WebTypeTool

__all__ = [
    "BrowserSession",
    "BrowserSessionStore",
    "ProxyPool",
    "WebClickTool",
    "WebOpenTool",
    "WebScrollTool",
    "WebSnapshotTool",
    "WebTypeTool",
    "load_proxy_pool",
]
