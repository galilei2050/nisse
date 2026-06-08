from app.chat.format import split_message, strip_markdown_v2, to_markdown_v2


def test_bold_becomes_single_asterisk():
    assert to_markdown_v2("**hi**") == "*hi*"


def test_italic_becomes_underscore():
    assert to_markdown_v2("*hi*") == "_hi_"


def test_header_becomes_bold():
    assert to_markdown_v2("## Title") == "*Title*"


def test_plain_specials_are_escaped():
    assert to_markdown_v2("a.b!c") == "a\\.b\\!c"


def test_fenced_code_body_not_escaped():
    out = to_markdown_v2("```\na.b!\n```")
    assert "a.b!" in out  # contents left intact
    assert "\\." not in out


def test_inline_code_not_escaped():
    out = to_markdown_v2("use `a.b()` now")
    assert "`a.b()`" in out


def test_bullet_list_not_eaten_by_italic():
    # The italic regex must stop at newlines so "* item" lines survive as bullets.
    out = to_markdown_v2("* one\n* two")
    assert "_" not in out
    assert "one" in out and "two" in out


def test_link_display_escaped_url_intact():
    out = to_markdown_v2("[a.b](http://x.com/a_b)")
    assert "[a\\.b](http://x.com/a_b)" == out


def test_short_text_is_single_chunk():
    assert split_message("hello") == ["hello"]


def test_paragraphs_split_without_mid_paragraph_cut():
    para = "x" * 3000
    chunks = split_message(f"{para}\n\n{para}", limit=4096)
    assert len(chunks) == 2
    assert all(len(c.encode("utf-16-le")) // 2 <= 4096 for c in chunks)
    assert chunks[0] == para


def test_oversized_paragraph_falls_to_sentences():
    block = " ".join(["Sentence number {0}.".format(i) for i in range(600)])
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
