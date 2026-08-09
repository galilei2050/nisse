"""A citation counts only if the run opened the page.

Production measured over 30 days: the retrieval worker cited 1,307 sources and had loaded 295 —
22.6%. A prompt rule moved one model (36% -> 64%) and not another (15% -> 13%), so the check has to
be arithmetic. It discloses rather than refuses: a search-result claim is sometimes legitimate;
silence about it never is.
"""

from app.subagents.citations import Citations


def test_a_source_the_run_never_opened_is_named() -> None:
    answer = "The rate is 9.375% (cdtfa.ca.gov/rates.pdf) and the county has 42 shops (census.gov/qf)."

    citations = Citations(answer, opened=["https://cdtfa.ca.gov/rates.pdf"])

    assert citations.read == 1
    assert citations.unread == {"census.gov/qf"}
    assert "census.gov/qf" in citations.demand()


def test_the_demand_offers_an_acceptable_exit() -> None:
    """An instruction with no way out gets worked around: dropping the claim must also be allowed."""
    demand = Citations("see census.gov/qf", opened=[]).demand()

    assert "Open them" in demand
    assert "drop" in demand


def test_an_answer_whose_every_source_was_read_passes() -> None:
    citations = Citations("see cdtfa.ca.gov/rates.pdf", opened=["https://cdtfa.ca.gov/rates.pdf/"])

    assert citations.unread == set()


def test_punctuation_and_scheme_do_not_make_a_second_source() -> None:
    """gpt-oss glues a CJK bracket to the url; scoring that as unread would measure punctuation."""
    answer = "as published【https://www.irs.gov/pub/i1065.pdf】, and again at www.irs.gov/pub/i1065.pdf."

    citations = Citations(answer, opened=["http://www.irs.gov/pub/i1065.pdf"])

    assert citations.cited == {"www.irs.gov/pub/i1065.pdf"}
    assert citations.unread == set()


def test_a_section_of_an_opened_page_counts_as_read() -> None:
    """The worker cites the anchor it quoted; the run opened the page that contains it."""
    citations = Citations("see caltrain.com/station/mountainview#north", opened=["https://caltrain.com/station"])

    assert citations.unread == set()
