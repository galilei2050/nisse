"""An answer that cites a page the run never opened must go back for another turn.

The loop's exit is the last place this is still fixable: the worker still has its tools and its turn
budget. A note handed to the caller instead names no claim and buys no fix.
"""

import pytest
from baski.agents.judge import Judge, Verdict

from app.subagents.citations import CitedJudge, OpenedPages


class _Stub(Judge):
    """A completeness judge that always passes, so only the citation check can fail the answer."""

    async def evaluate(self, transcript: str, answer: str, rules: str) -> Verdict:
        """Say the answer is complete, whatever it says."""
        return Verdict(finished=True, missing=[], feedback="")


class _Browse:
    """Stands in for the real browse tool; `OpenedPages` only needs its identity and its call."""

    name = "browse_website"
    description = "Read a page."
    one_line = "Read a page."
    Input = Verdict  # any model — the wrapper copies it and nothing here inspects it

    async def execute(self, **kwargs: object) -> str:
        """Return the page as the real tool would."""
        return "page text"


async def _verdict(answer: str, opened_urls: list[str]) -> Verdict:
    """Grade `answer` after a run that opened `opened_urls`."""
    recorder = OpenedPages(_Browse())  # type: ignore[arg-type]  # duck-typed stand-in
    for url in opened_urls:
        await recorder.execute(url=url)
    return await CitedJudge(_Stub(), [recorder]).evaluate(transcript="q", answer=answer, rules="r")


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
