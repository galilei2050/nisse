"""Did the answer cite pages the run actually opened?

The one check on this axis that needs no model. Measured over 30 days of production: the retrieval
worker cited 1,307 sources and had loaded 295 of them — 22.6% — and 93 answers cited three or more
sources having opened none. A prompt rule fixed that for one model (36% -> 64%) and did nothing for
another (15% -> 13%), which is why this is arithmetic and not an instruction.

It sends the answer back rather than annotating it. A note travelling upward names no claim and buys
no fix; at the loop's exit the worker still has its tools, and the demand offers an exit — open the
page, or drop the claim and say the evidence is thin.
"""

import logging
import re

from baski.agents import ToolResult
from baski.agents.judge import Judge, Verdict
from baski.agents.tool import Tool
from pydantic import BaseModel

# Answers cite bare hosts as often as full links, so requiring the scheme scored a well-sourced
# answer as having none at all.
_URL = re.compile(
    r"(?:https?://)?(?:[a-z0-9-]+\.)+(?:com|org|gov|net|edu|io|co|us|uk|ca|ru|info|biz)"
    r"(?:/[^\s\]\)>\"'`,;】》」＞]*)?",
    re.IGNORECASE,
)
# gpt-oss closes a citation with a CJK bracket glued to the url, and any model may end a sentence
# right after one; scoring those as different urls would measure punctuation.
_TRAILING = ".,;:!?)]}>】》」＞"

logger = logging.getLogger(__name__)

# `WebBrowseTool` reports a dead url as text rather than raising, so these openings are how a failed
# fetch is told apart from a page. A url that answered with one of them was never read.
_FETCH_FAILED = ("Website timed out", "Cannot access website", "Website not found", "Website returned HTTP")


def _normalise(url: str) -> str:
    """Compare urls without the parts that do not identify a page."""
    return url.rstrip(_TRAILING).removeprefix("https://").removeprefix("http://").rstrip("/").lower()


class OpenedPages(Tool):
    """Wraps `browse_website` and remembers which urls it was asked for.

    The transcript also records each call (`[tool] browse_website({...})`) and the judge is handed it,
    so this is not the only place the set exists — it is the only place it stays complete. History
    truncation drops the oldest turns at 90% of budget, and a page read early in a long run would
    vanish from the transcript while its citation remained, turning a read source into a demand to
    re-open it. Lifecycle: one per sub-agent run, since the list is that run's evidence.
    """

    class Input(BaseModel):
        """Stub, required by `Tool.__init_subclass__`; the wrapped tool's schema replaces it.

        Both halves have to be copied in `__init__`: `Input` is what validates the call, while
        `input_schema` — derived from THIS class at definition time — is what the model is shown.
        Copying only `Input` left the model looking at a `browse_website` with `url` alone, so
        `sections` and `offset` became uncallable while the description still told it to use them.
        """

        url: str

    def __init__(self, browse: Tool) -> None:
        """Take the wrapped tool's identity, schema and all, so the model sees no difference."""
        self.name = browse.name
        self.description = browse.description
        self.one_line = browse.one_line
        self.Input = browse.Input  # type: ignore[misc]  # validates the call
        self.input_schema = browse.input_schema  # what the model is shown
        self._browse = browse
        self.urls: list[str] = []

    async def execute(self, url: str, **kwargs: object) -> str | ToolResult:
        """Fetch first, and record the url only if the page came back.

        Recording the request would let a hallucinated link that 404s count as read — the failure
        this whole check exists to catch. `WebBrowseTool` returns its errors as text rather than
        raising, so the result is what says whether there was a page.
        """
        result = await self._browse.execute(url=url, **kwargs)
        content = result if isinstance(result, str) else result.content
        if not content.startswith(_FETCH_FAILED):
            self.urls.append(url)
        return result


class CitedJudge(Judge):
    """Grades one thing a model cannot: was the cited page actually opened.

    Sits on the jury beside the completeness judge, at the loop's exit — so an answer citing a page
    the run never opened goes back for another turn instead of travelling upward with a note
    attached. A count handed to the caller is not actionable: it names no claim and buys no fix. Here
    the worker still has its tools and its budget, and one more turn on a cheap model is worth less
    than a source the owner cannot check.

    Lifecycle: one per sub-agent run — it reads that run's recorders.
    """

    def __init__(self, opened: list[OpenedPages]) -> None:
        """Bind the recorders holding what this run loaded."""
        self._opened = opened

    async def evaluate(self, transcript: str, answer: str, rules: str) -> Verdict:  # noqa: ARG002 — panel signature
        """Pass unless the answer cites something this run never read."""
        citations = Citations(answer, [url for recorder in self._opened for url in recorder.urls])
        if not citations.unread:
            return Verdict(finished=True, missing=[], feedback="")
        logger.info(
            "Answer cites sources it never opened",
            extra={"cited": len(citations.cited), "read": citations.read},
        )
        return Verdict(finished=False, missing=[citations.demand()], feedback=citations.demand())


class Citations:
    """What one answer cited, against what its run actually loaded."""

    def __init__(self, answer: str, opened: list[str]) -> None:
        """Extract the cited urls and match them against the pages the run opened."""
        self._opened = {_normalise(url) for url in opened}
        self.cited = {_normalise(url) for url in _URL.findall(answer)}
        self.unread = {url for url in self.cited if not self._matches(url)}

    def _matches(self, cited: str) -> bool:
        """Read when the citation IS an opened url, or a fragment/sub-path of an opened PATH.

        The boundary matters and a plain `startswith` in both directions does not have it: opening
        `irs.gov/` normalises to the bare host, and every invented `irs.gov/pub/whatever` would then
        vouch for itself. An opened url can only stand for something below it, and only if it names
        a page rather than a whole site.
        """
        return any(
            cited == opened or (("/" in opened and cited.startswith(f"{opened}/")) or cited.startswith(f"{opened}#"))
            for opened in self._opened
        )

    @property
    def read(self) -> int:
        """How many of the cited sources were actually opened."""
        return len(self.cited) - len(self.unread)

    def demand(self) -> str:
        """What the worker must do about the sources it cited without reading.

        Phrased as a choice, not only a prohibition: opening the page and dropping the claim are both
        acceptable, and an instruction with no acceptable exit gets worked around instead of obeyed.
        """
        listed = ", ".join(sorted(self.unread)[:5])
        return (
            f"{len(self.unread)} of {len(self.cited)} cited sources were never opened with "
            f"`browse_website`: {listed}. Open them and keep only what they actually say, or drop "
            f"the claims that rest on them and say the evidence is thin."
        )
