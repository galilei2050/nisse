"""Where each chat's browser session lives.

`make startbrowser U=<chat-id>` opens a real browser; you log in once and the session (cookies +
localStorage) is written here as a Playwright storage-state file, keyed by chat-id. The assistant
loads the same file for that chat, so it acts with your logins. One cookie jar per chat — the same
per-conversation scoping every other store uses. Override the directory with BROWSER_STATE_DIR.
"""

from pathlib import Path

from baski.env import get_env

_DEFAULT_DIR = Path.home() / ".nisse" / "browser_state"


def browser_state_path(conversation_id: int) -> Path:
    """Path to the storage-state file for one chat — written by `make startbrowser`, read by the agent."""
    state_dir = Path(str(get_env("BROWSER_STATE_DIR", str(_DEFAULT_DIR))))
    return state_dir / f"{conversation_id}.json"
