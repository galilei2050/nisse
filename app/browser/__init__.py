"""Browser actions — a per-chat page the agent reads as an element listing and acts on.

**Registered, but held by nobody.** `register_tools` puts the five actions in the shared registry under
the name `browser`; `MAIN_TOOLS` does not list it and no sub-agent config names it, so today nothing
can call them. Registration exists so the nightly curator is *able* to grant them: `subagent_save`
validates `tool_names` against this registry, and a tool that isn't in it cannot be handed to a worker
however plainly the evidence says the worker needs it.

Nothing writes the `browser_sessions` collection yet, so `BrowserSessionStore.load` returns None and
every session opens logged out — fine for a public page, useless behind a login.

Design, the bot-protection findings and the open defects to fix before this is leaned on:
`docs/browser-actions.md`.
"""

from app.browser.proxy import ProxyPool, load_proxy_pool
from app.browser.session import BrowserSession
from app.browser.store import BrowserSessionStore
from app.browser.tools import (
    WebClickTool,
    WebOpenTool,
    WebScrollTool,
    WebSnapshotTool,
    WebTypeTool,
    browser_tools,
    register_tools,
)

__all__ = [
    "BrowserSession",
    "BrowserSessionStore",
    "ProxyPool",
    "WebClickTool",
    "WebOpenTool",
    "WebScrollTool",
    "WebSnapshotTool",
    "WebTypeTool",
    "browser_tools",
    "load_proxy_pool",
    "register_tools",
]
