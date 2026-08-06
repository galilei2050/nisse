from baski.agents import AgentExecuteResult, Verdict

from app.chat.format import compose_answer, split_message, strip_markdown_v2, to_markdown_v2


def test_empty_passthrough():
    assert to_markdown_v2("") == ""


def test_converts_bold_to_mdv2():
    # Smoke: the library does the work; we only assert it converted (no raw **).
    assert to_markdown_v2("**hi**") == "*hi*"


def test_thematic_break_is_dropped():
    # Our value-add on top of the library: --- separators are removed entirely.
    out = to_markdown_v2("para1\n\n---\n\npara2")
    assert "—" not in out
    assert "-" not in out
    assert "para1" in out and "para2" in out


def test_special_chars_escaped():
    assert to_markdown_v2("a.b!c") == "a\\.b\\!c"


def test_short_text_is_single_chunk():
    assert split_message("hello") == ["hello"]


def test_paragraphs_split_without_mid_paragraph_cut():
    para = "x" * 3000
    chunks = split_message(f"{para}\n\n{para}", limit=4096)
    assert len(chunks) == 2
    assert all(len(c.encode("utf-16-le")) // 2 <= 4096 for c in chunks)
    assert chunks[0] == para


def test_oversized_paragraph_falls_to_sentences():
    block = " ".join(f"Sentence number {i}." for i in range(600))
    chunks = split_message(block, limit=4096)
    assert len(chunks) > 1
    assert all(len(c.encode("utf-16-le")) // 2 <= 4096 for c in chunks)


def test_code_block_kept_atomic():
    code = "```\n" + "\n".join(f"line {i}" for i in range(50)) + "\n```"
    chunks = split_message(f"intro\n\n{code}\n\ntail", limit=4096)
    assert any(c.count("```") == 2 for c in chunks)  # the fence stays whole in one chunk


def test_strip_roundtrips_to_plain():
    assert strip_markdown_v2(to_markdown_v2("**bold** and a.b")) == "bold and a.b"


def test_strip_keeps_snake_case():
    assert strip_markdown_v2("my_variable_name") == "my_variable_name"


def _result(*verdicts: Verdict) -> AgentExecuteResult:
    return AgentExecuteResult(
        trace_id="t",
        response="Готово.",
        total_input_tokens=100,
        total_output_tokens=10,
        turn_count=1,
        tool_call_count=0,
        total_cost=0.1234,
        context_tokens=8000,
        judge_verdicts=list(verdicts),
    )


_PASSED = Verdict(finished=True, missing=[], feedback="")
_SENT_BACK = Verdict(finished=False, missing=["источники"], feedback="Назови источники.")


def test_a_composed_answer_is_the_reply_then_the_verdict_then_the_cost():
    """The live path shows both beside the answer; a scheduled reply or a curator report reaches the
    owner with no stream behind it, and without these they cannot tell a checked answer from an
    unchecked one. Asserted whole — the order is the message, and a verdict above the answer reads
    as a comment on nothing.
    """
    assert compose_answer(_result(_PASSED)) == "Готово.\n\n**⚖️ ✅ готово**\n\n— $0.1234 · контекст 8k"


def test_the_verdict_shown_is_the_one_the_delivered_answer_earned():
    """A redo leaves several verdicts: the first sent the draft back, the last graded what is being
    delivered. Showing the first would put 🔄 and its "what's missing" on an answer that has it.
    """
    assert "**⚖️ ✅ готово**" in compose_answer(_result(_SENT_BACK, _PASSED))


def test_an_answer_the_judge_kept_rejecting_says_what_it_wanted():
    """The retry cap can run out — then the last verdict really is a rejection, and the owner is told
    what the judge was still asking for rather than being shown a ✅ it never gave.
    """
    assert "**⚖️ 🔄 Назови источники.**" in compose_answer(_result(_PASSED, _SENT_BACK))


def test_an_ungraded_run_gets_no_verdict_line():
    """The judge fails open, so a Vertex outage leaves no verdict — inventing a ✅ there would tell
    the owner the answer was checked when nothing checked it.
    """
    assert compose_answer(_result()) == "Готово.\n\n— $0.1234 · контекст 8k"
