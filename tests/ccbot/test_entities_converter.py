"""Tests for entities-based markdown conversion and splitting."""

from __future__ import annotations

import time

from telegram import MessageEntity

from ccbot.entities_converter import (
    RenderedMessage,
    render_markdown_to_entities,
    split_text_and_entities,
)
from ccbot.transcript_parser import TranscriptParser


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _validate_chunk_entities(text: str, entities: list[MessageEntity]) -> None:
    total_utf16 = _utf16_len(text)
    for entity in entities:
        assert 0 <= entity.offset <= total_utf16
        assert entity.length > 0
        assert entity.offset + entity.length <= total_utf16


class TestRenderMarkdownToEntities:
    def test_returns_dataclass_payload(self) -> None:
        rendered = render_markdown_to_entities("hello")
        assert isinstance(rendered, RenderedMessage)
        assert "hello" in rendered.text

    def test_basic_formatting_entities(self) -> None:
        rendered = render_markdown_to_entities(
            "**bold** _italic_ ~~strike~~ [link](https://example.com) ||spoiler||"
        )
        types = {str(entity.type) for entity in rendered.entities}
        assert "bold" in types
        assert "italic" in types
        assert "strikethrough" in types
        assert "text_link" in types
        assert "spoiler" in types

    def test_code_block_keeps_only_pre_for_same_range(self) -> None:
        rendered = render_markdown_to_entities("```python\nprint('x')\n```")
        pre_entities = [e for e in rendered.entities if e.type == MessageEntity.PRE]
        code_entities = [e for e in rendered.entities if e.type == MessageEntity.CODE]
        assert pre_entities
        # sulguk can emit code + pre for same range; converter should keep only pre
        for code_entity in code_entities:
            assert (code_entity.offset, code_entity.length) not in {
                (pre_entity.offset, pre_entity.length) for pre_entity in pre_entities
            }

    def test_expandable_quote_sentinels_are_converted(self) -> None:
        text = (
            f"before {TranscriptParser.EXPANDABLE_QUOTE_START}inside"
            f"{TranscriptParser.EXPANDABLE_QUOTE_END} after"
        )
        rendered = render_markdown_to_entities(text)
        types = {str(entity.type) for entity in rendered.entities}
        assert "expandable_blockquote" in types
        assert TranscriptParser.EXPANDABLE_QUOTE_START not in rendered.text
        assert TranscriptParser.EXPANDABLE_QUOTE_END not in rendered.text

    def test_quote_then_list_does_not_crash(self) -> None:
        rendered = render_markdown_to_entities("> quote\n- item 1\n- item 2\n")
        assert "quote" in rendered.text
        assert "item" in rendered.text

    def test_nested_backticks_in_fenced_code_does_not_crash(self) -> None:
        rendered = render_markdown_to_entities("""```python
code = '''```'''
print(code)
```""")
        assert "code =" in rendered.text
        assert any(entity.type == MessageEntity.PRE for entity in rendered.entities)

    def test_fenced_code_with_html_does_not_disable_markdown(self) -> None:
        rendered = render_markdown_to_entities(
            "## Заголовок\n\n```python\nreturn '<div class=\"x\">'</div>'\n```"
        )
        types = {str(entity.type) for entity in rendered.entities}
        assert "bold" in types
        assert "pre" in types

    def test_bracket_heavy_text_converts_without_hang(self) -> None:
        text = "[" * 6000 + "x" + "]" * 6000
        started = time.perf_counter()
        rendered = render_markdown_to_entities(text)
        elapsed = time.perf_counter() - started
        assert rendered.text
        # Safety guard against pathological blowups in test environment.
        assert elapsed < 2.0

    def test_utf16_offsets_for_emoji(self) -> None:
        rendered = render_markdown_to_entities("😀 **x**")
        bold_entities = [entity for entity in rendered.entities if entity.type == MessageEntity.BOLD]
        assert bold_entities
        bold = bold_entities[0]
        # "😀 " is 3 UTF-16 units (emoji surrogate pair + space)
        assert bold.offset == 3

    def test_preserves_single_newlines_in_plain_text(self) -> None:
        rendered = render_markdown_to_entities("line1\nline2\nline3")
        assert "line1\nline2\nline3" in rendered.text

    def test_preserves_read_summary_newline(self) -> None:
        text = "Read(/home/vadim/file.py)\n⎿ Read 456 lines"
        rendered = render_markdown_to_entities(text)
        assert "Read(/home/vadim/file.py)\n⎿ Read 456 lines" in rendered.text

    def test_raw_u_tag_maps_to_underline_entity(self) -> None:
        rendered = render_markdown_to_entities("<u>underline</u>")
        assert any(entity.type == MessageEntity.UNDERLINE for entity in rendered.entities)


class TestSplitTextAndEntities:
    def test_returns_single_chunk_when_short(self) -> None:
        rendered = render_markdown_to_entities("hello **world**")
        chunks = split_text_and_entities(rendered.text, rendered.entities, max_chars=100)
        assert len(chunks) == 1

    def test_splits_long_text_and_validates_offsets(self) -> None:
        rendered = render_markdown_to_entities(" ".join(["**bold**"] * 1500))
        chunks = split_text_and_entities(rendered.text, rendered.entities, max_chars=500)
        assert len(chunks) > 1
        for chunk_text, chunk_entities in chunks:
            assert len(chunk_text) <= 500
            _validate_chunk_entities(chunk_text, chunk_entities)

    def test_clips_entity_crossing_chunk_boundary(self) -> None:
        text = "A" * 20
        entities = [MessageEntity(type=MessageEntity.BOLD, offset=0, length=20)]
        chunks = split_text_and_entities(text, entities, max_chars=7)
        assert len(chunks) >= 3
        for chunk_text, chunk_entities in chunks:
            _validate_chunk_entities(chunk_text, chunk_entities)

    def test_rejects_invalid_max_chars(self) -> None:
        try:
            split_text_and_entities("abc", [], max_chars=0)
        except ValueError:
            pass
        else:
            raise AssertionError("Expected ValueError for max_chars <= 0")
