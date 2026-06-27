"""Managed remote browser (Browserbase) — the CDP endpoint the agent attaches to instead of local Chromium.

Lets the agent act on bot-protected sites (DoorDash) whose Cloudflare Turnstile rejects an automated
local browser. Validated: a Browserbase session clears Turnstile and its cart writes stick, where a
local headless browser's are silently dropped. See docs/browser-actions.md.

Optional: set BROWSERBASE_API_KEY + BROWSERBASE_PROJECT_ID to route the agent through the managed
browser; leave them unset to launch a local Chromium (fine for dev and unprotected sites).
"""

import os

from baski.server import Logger
from browserbase import Browserbase


def managed_browser_cdp_url(logger: Logger) -> str | None:
    """A fresh Browserbase session's CDP connect URL, or None when Browserbase isn't configured."""
    api_key = os.environ.get("BROWSERBASE_API_KEY")
    project_id = os.environ.get("BROWSERBASE_PROJECT_ID")
    if not (api_key and project_id):
        return None
    session = Browserbase(api_key=api_key).sessions.create(project_id=project_id)
    logger.info("Browserbase session created", labels={"sessionId": session.id})
    return session.connect_url
