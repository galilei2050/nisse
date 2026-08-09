"""Did the answer cite pages the run actually opened?

The one check on this axis that needs no model. Measured over 30 days of production: the retrieval
worker cited 1,307 sources and had loaded 295 of them — 22.6% — and 93 answers cited three or more
sources having opened none. A prompt rule fixed that for one model (36% -> 64%) and did nothing for
another (15% -> 13%), which is why this is arithmetic and not an instruction.

It discloses rather than refuses: a claim resting on a search result is sometimes legitimate, and
breaking that answer would cost more than it saves. What the caller must not have is silence — an
invented figure under a real-looking link is the failure the owner cannot detect.
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


def _normalise(url: str) -> str:
    """Compare urls without the parts that do not identify a page."""
    return url.rstrip(_TRAILING).removeprefix("https://").removeprefix("http://").rstrip("/").lower()


class OpenedPages(Tool):
    """Wraps `browse_website` and remembers which urls it was asked for.

    baski's `ToolSet` keeps each call's cost and duration but not its arguments, and the set of pages
    a run opened is knowable only here — adding it to the library for one consumer would be the wrong
    place. Lifecycle: one per sub-agent run, since the list is that run's evidence.
    """

    class Input(BaseModel):
        """Placeholder schema, replaced per instance.

        `Tool` demands one on the class; the real schema is the wrapped tool's, copied in `__init__`
        before the model is ever shown this.
        """

        url: str

    def __init__(self, browse: Tool) -> None:
        """Take the wrapped tool's identity so the model sees no difference."""
        self.name = browse.name
        self.description = browse.description
        self.one_line = browse.one_line
        self.Input = browse.Input  # type: ignore[misc]  # the schema the model is shown
        self._browse = browse
        self.urls: list[str] = []

    async def execute(self, **kwargs: object) -> str | ToolResult:
        """Record the url, then hand the call to the real tool unchanged."""
        url = kwargs.get("url")
        if isinstance(url, str):
            self.urls.append(url)
        return await self._browse.execute(**kwargs)


class CitedJudge(Judge):
    """The completeness judge, plus one verdict it cannot reach: was the source actually read.

    Sits where the judge sits — at the loop's exit — so an answer citing a page the run never opened
    is sent back for another turn instead of travelling upward with a note attached. A count handed
    to the caller is not actionable: it names no claim and buys no fix. Here the worker still has its
    tools and its budget, and one more turn on a cheap model is worth less than a source the owner
    cannot check.

    Lifecycle: one per sub-agent run — it reads that run's recorders.
    """

    def __init__(self, judge: Judge, opened: list[OpenedPages]) -> None:
        """Bind the real judge and the recorders holding what this run loaded."""
        self._judge = judge
        self._opened = opened

    async def evaluate(self, transcript: str, answer: str, rules: str) -> Verdict:
        """Grade completeness first; an otherwise-finished answer still fails on an unread source."""
        verdict = await self._judge.evaluate(transcript=transcript, answer=answer, rules=rules)
        citations = Citations(answer, [url for recorder in self._opened for url in recorder.urls])
        if not citations.unread:
            return verdict
        logger.info(
            "Answer cites sources it never opened",
            extra={"cited": len(citations.cited), "read": citations.read},
        )
        return Verdict(
            finished=False,
            missing=[*verdict.missing, citations.demand()],
            feedback=verdict.feedback,
        )


class Citations:
    """What one answer cited, against what its run actually loaded."""

    def __init__(self, answer: str, opened: list[str]) -> None:
        """Extract the cited urls and match them against the pages the run opened."""
        self._opened = {_normalise(url) for url in opened}
        self.cited = {_normalise(url) for url in _URL.findall(answer)}
        self.unread = {url for url in self.cited if not self._matches(url)}

    def _matches(self, cited: str) -> bool:
        """A citation counts as read when it names a page the run loaded, or that page's section."""
        return any(cited.startswith(opened) or opened.startswith(cited) for opened in self._opened)

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
