"""An action asked for before any page was opened must refuse, not invent a blank page.

The only piece of `BrowserSession` reachable without a browser, and worth the test for that reason: the
refusal returns before anything touches the Playwright client or the Mongo store, so nothing is faked.

What it defends, measured in production: the acting methods used to go through the lazy open, which
created `about:blank`, and the tools then answered "0 interactive elements". The worker read that as "the
page is empty or still loading" and reported it to the owner instead of opening anything.
"""

from typing import cast

import pytest

from app.browser.session import BrowserSession, NoPageOpenError
from app.browser.store import BrowserSessionStore
from app.browser.tools import WebClickTool, WebScrollTool, WebSnapshotTool, WebTypeTool


def _session() -> BrowserSession:
    """A session that has never opened a page. Neither collaborator is reachable on the refusal path."""
    return BrowserSession(
        client=cast("object", None),  # type: ignore[arg-type]  # unused: the refusal returns before any call
        session_store=cast("BrowserSessionStore", None),
    )


@pytest.mark.parametrize("action", ["snapshot", "click", "type", "scroll"])
@pytest.mark.asyncio
async def test_every_acting_method_refuses_without_an_open_page(action: str) -> None:
    """Only `open` may create a page; the four that act on one must say there is none."""
    session = _session()
    arguments = {"click": {"ref": 0}, "type": {"ref": 0, "text": "x", "submit": False}}.get(action, {})
    with pytest.raises(NoPageOpenError):
        await getattr(session, action)(**arguments)


def test_the_refusal_carries_the_words_the_model_reads() -> None:
    """The message lives on the exception, so it is not caught and re-phrased at four call sites.

    Letting it raise is what buys the traceback and `is_error=True` from baski's tool loop; the model reads
    these words either way.
    """
    assert str(NoPageOpenError()) == "No page is open in this session. Call web_open with a URL first."


@pytest.mark.asyncio
async def test_the_acting_tools_let_the_refusal_out_instead_of_returning_it() -> None:
    """Catching it would trade the traceback and the error mark for words the exception already carries.

    A returned sentence is a SUCCESSFUL tool result to baski's loop, so the run records a failed action as
    a completed one and nothing is logged. Asserted through `execute`, the boundary baski actually calls.
    """
    session = _session()
    for tool, arguments in (
        (WebSnapshotTool(session), {}),
        (WebClickTool(session), {"ref": 0}),
        (WebTypeTool(session), {"ref": 0, "text": "x", "submit": False}),
        (WebScrollTool(session), {}),
    ):
        with pytest.raises(NoPageOpenError):
            await tool.execute(**arguments)
