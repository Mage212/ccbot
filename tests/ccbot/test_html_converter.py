"""Tests for HTML markdown conversion using chatgpt-md-converter."""

from ccbot.html_converter import convert_markdown, split_message
from ccbot.transcript_parser import TranscriptParser

EXP_START = TranscriptParser.EXPANDABLE_QUOTE_START
EXP_END = TranscriptParser.EXPANDABLE_QUOTE_END


class TestConvertMarkdown:
    def test_plain_text(self) -> None:
        result = convert_markdown("hello world")
        assert "hello world" in result

    def test_bold(self) -> None:
        result = convert_markdown("**bold text**")
        assert "<b>bold text</b>" in result

    def test_italic(self) -> None:
        result = convert_markdown("*italic text*")
        assert "<i>italic text</i>" in result

    def test_code_inline(self) -> None:
        result = convert_markdown("`inline code`")
        assert "<code>inline code</code>" in result

    def test_code_block(self) -> None:
        result = convert_markdown("```python\nprint('hi')\n```")
        assert "<pre>" in result
        assert "print" in result

    def test_link(self) -> None:
        result = convert_markdown("[link text](https://example.com)")
        assert '<a href="https://example.com">link text</a>' in result

    def test_expandable_quote_sentinels(self) -> None:
        text = f"{EXP_START}quoted content{EXP_END}"
        result = convert_markdown(text)
        assert EXP_START not in result
        assert EXP_END not in result
        assert "<blockquote expandable>" in result
        assert "</blockquote>" in result

    def test_mixed_text_and_expandable_quote(self) -> None:
        text = f"before {EXP_START}inside quote{EXP_END} after"
        result = convert_markdown(text)
        assert EXP_START not in result
        assert EXP_END not in result
        assert "<blockquote expandable>" in result
        assert "before" in result
        assert "after" in result

    def test_nested_backticks_in_code_block(self) -> None:
        """Test that triple backticks inside code blocks are replaced."""
        # This tests the _preprocess_nested_backticks function
        code_block = """```python
code = '''```'''
```"""
        result = convert_markdown(code_block)
        # Should have a pre tag and the content
        assert "<pre>" in result


class TestSplitMessage:
    def test_short_text(self) -> None:
        result = split_message("short")
        assert result == ["short"]

    def test_short_text_returns_list(self) -> None:
        """Short text should still be returned as a list."""
        result = split_message("short text", max_length=100)
        assert isinstance(result, list)
        assert len(result) == 1

    def test_long_text_splits(self) -> None:
        long_text = "a" * 5000
        result = split_message(long_text)
        assert len(result) > 1
        assert all(len(chunk) <= 4096 for chunk in result)

    def test_respects_custom_max_length(self) -> None:
        # chatgpt-md-converter has MIN_LENGTH=500, so use a larger text
        long_text = "a" * 1000
        result = split_message(long_text, max_length=600)
        assert all(len(chunk) <= 600 for chunk in result)

    def test_empty_text(self) -> None:
        result = split_message("")
        assert result == [""]

    def test_multiple_chunks_content_preserved(self) -> None:
        """Core content should be preserved across chunks (HTML tags added)."""
        text = "word " * 2000  # ~10000 chars
        result = split_message(text)
        # Each chunk should contain the word, though HTML tags may be added
        for chunk in result:
            assert "word" in chunk


class TestStripSentinels:
    def test_strip_sentinels(self) -> None:
        from ccbot.html_converter import strip_sentinels

        text = f"before {EXP_START}content{EXP_END} after"
        result = strip_sentinels(text)
        assert EXP_START not in result
        assert EXP_END not in result
        assert "before" in result
        assert "content" in result
        assert "after" in result

    def test_strip_sentinels_no_sentinels(self) -> None:
        from ccbot.html_converter import strip_sentinels

        text = "plain text without sentinels"
        result = strip_sentinels(text)
        assert result == text
