from app.chat.format import split_message, strip_markdown_v2, to_markdown_v2


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
