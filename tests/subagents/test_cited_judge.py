"""An answer that cites a page the run never opened must go back for another turn.

The loop's exit is the last place this is still fixable: the worker still has its tools and its turn
budget. A note handed to the caller instead names no claim and buys no fix.
"""

import pytest
from baski.agents.judge import Verdict

from app.subagents.citations import CitedJudge, OpenedPages


class _Browse:
    """Stands in for the real browse tool; `OpenedPages` only needs its identity and its call."""

    name = "browse_website"
    description = "Read a page."
    one_line = "Read a page."
    Input = Verdict  # any model — the wrapper copies it and nothing here inspects it
    input_schema = {"type": "object", "properties": {"url": {"type": "string"}}}

    def __init__(self, *, dead: str = "") -> None:
        """`dead` names a url this stand-in refuses, the way a 404 does — as text, not a raise."""
        self._dead = dead

    async def execute(self, url: str, **kwargs: object) -> str:
        """Return page text, or the refusal the real tool returns for a url that does not exist."""
        return f"Website not found (404). URL does not exist: {url}" if url == self._dead else "page text"


async def _verdict(answer: str, opened_urls: list[str], dead: str = "") -> Verdict:
    """Grade `answer` on the citation axis alone, after a run that opened `opened_urls`.

    Completeness is a separate member of the jury (`baski.agents.Jury`); this judge answers only
    "was the cited page read", so the test needs no stand-in for the other one.
    """
    recorder = OpenedPages(_Browse(dead=dead))  # type: ignore[arg-type]  # duck-typed stand-in
    for url in opened_urls:
        await recorder.execute(url=url)
    return await CitedJudge([recorder]).evaluate(transcript="q", answer=answer, rules="r")


@pytest.mark.asyncio
async def test_an_unopened_citation_fails_an_otherwise_complete_answer() -> None:
    verdict = await _verdict("the rate is 9.375% (census.gov/qf)", opened_urls=[])

    assert verdict.finished is False
    assert "census.gov/qf" in verdict.missing[0]


@pytest.mark.asyncio
async def test_an_answer_citing_only_what_it_read_is_left_alone() -> None:
    verdict = await _verdict("the rate is 9.375% (cdtfa.ca.gov/rates)", opened_urls=["https://cdtfa.ca.gov/rates"])

    assert verdict.finished is True
    assert verdict.missing == []


@pytest.mark.asyncio
async def test_an_answer_with_no_citations_is_not_failed_by_this_check() -> None:
    """Not every sub-question needs a link; only a CITED source has to have been read."""
    verdict = await _verdict("the evidence is thin and I found nothing solid", opened_urls=[])

    assert verdict.finished is True
