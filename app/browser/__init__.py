"""Logged-in browser actions — a per-chat session the agent reads as an element listing and acts on.

Ported from the `feat/browser-session` branch (nisse #34) as the tools alone. **Nothing here is wired
yet:** no `register_tools`, so the shared registry (`app/tools/`) does not know these names and no
agent — main or sub — can call them. Building a `BrowserSession` is the caller's job when the wiring
lands; until then this package is capability sitting on the shelf, deliberately.

What #34 also had and this does NOT: the managed remote browser (Browserbase over CDP — needed to
transact on Cloudflare-protected sites), `make startbrowser` (which is what WRITES a chat's session,
so `BrowserSessionStore.load` returns None until it exists), and the `CoreDeps`/`Conversations`
wiring. Design, research and the measured bot-protection findings: `docs/browser-actions.md`.
"""

from app.browser.proxy import ProxyPool, ProxyServer, load_proxy_pool, parse_proxies
from app.browser.session import BrowserSession
from app.browser.store import BrowserSessionStore
from app.browser.tools import WebClickTool, WebOpenTool, WebScrollTool, WebSnapshotTool, WebTypeTool

__all__ = [
    "BrowserSession",
    "BrowserSessionStore",
    "ProxyPool",
    "ProxyServer",
    "WebClickTool",
    "WebOpenTool",
    "WebScrollTool",
    "WebSnapshotTool",
    "WebTypeTool",
    "load_proxy_pool",
    "parse_proxies",
]
