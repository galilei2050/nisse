"""Logged-in browser actions — a per-chat session the agent reads (a11y tree) and acts on."""

from .managed import managed_browser_cdp_url
from .proxy import ProxyPool, load_proxy_pool
from .session import BrowserSession
from .store import BrowserSessionStore
from .tools import WebClickTool, WebOpenTool, WebScrollTool, WebSnapshotTool, WebTypeTool

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
    "managed_browser_cdp_url",
]
