"""Logged-in browser actions — a per-chat session the agent reads (a11y tree) and acts on."""

from .proxy import ProxyPool, load_proxy_pool
from .session import BrowserSession
from .tools import WebClickTool, WebOpenTool, WebSnapshotTool, WebTypeTool

__all__ = [
    "BrowserSession",
    "ProxyPool",
    "WebClickTool",
    "WebOpenTool",
    "WebSnapshotTool",
    "WebTypeTool",
    "load_proxy_pool",
]
