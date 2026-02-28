"""HTML-based markdown conversion and splitting using chatgpt_md_converter.

Drop-in replacement for markdown_v2.py using HTML output format instead of MarkdownV2.
Provides convert_markdown() and split_message() functions with compatible signatures.

When CCBOT_USE_HTML_CONVERTER=true, the bot uses HTML parse mode which allows
cleaner formatting and more reliable tag-aware message splitting.
"""

import logging

from chatgpt_md_converter import split_html_for_telegram, telegram_format

from .transcript_parser import TranscriptParser

logger = logging.getLogger(__name__)

# Re-export sentinel markers for compatibility
EXPANDABLE_QUOTE_START = TranscriptParser.EXPANDABLE_QUOTE_START
EXPANDABLE_QUOTE_END = TranscriptParser.EXPANDABLE_QUOTE_END


def _convert_expandable_quotes(text: str) -> str:
    """Convert expandable quote sentinel markers to HTML blockquote.

    Telegram supports <blockquote expandable> for collapsible quotes.
    """
    # Replace sentinels with HTML blockquote tag
    text = text.replace(EXPANDABLE_QUOTE_START, "<blockquote expandable>")
    text = text.replace(EXPANDABLE_QUOTE_END, "</blockquote>")
    return text


def _preprocess_nested_backticks(text: str) -> str:
    """Replace triple backticks inside code blocks with Unicode lookalikes.

    chatgpt_md_converter incorrectly interprets triple backticks inside code
    blocks as end-of-block markers. We replace them with U+02CB (MODIFIER LETTER
    GRAVE ACCENT) which looks identical but isn't parsed as markdown.

    Only replaces backticks that appear quoted inside code blocks:
      '```' -> 'ˋˋˋ'
      "```" -> "ˋˋˋ"
    """
    result = []
    lines = text.split("\n")
    in_code = False
    code_buffer: list[str] = []
    code_lang = ""

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") and not in_code:
            # Start of code block
            in_code = True
            code_lang = stripped[3:]
            code_buffer = []
        elif stripped == "```" and in_code:
            # End of code block - process content
            code_content = "\n".join(code_buffer)
            # Replace triple backticks in quoted strings
            # Order matters: longer patterns first
            code_content = code_content.replace("'''```'''", "'''ˋˋˋ'''")
            code_content = code_content.replace('"""```"""', '"""ˋˋˋ"""')
            code_content = code_content.replace("'```'", "'ˋˋˋ'")
            code_content = code_content.replace('"```"', '"ˋˋˋ"')
            result.append(f"```{code_lang}")
            result.append(code_content)
            result.append("```")
            in_code = False
        elif in_code:
            code_buffer.append(line)
        else:
            result.append(line)

    return "\n".join(result)


def convert_markdown(text: str) -> str:
    """Convert Markdown to Telegram HTML format.

    Drop-in replacement for markdown_v2.convert_markdown().
    Handles:
      - Nested backticks in code blocks
      - Expandable quote sentinels
      - Standard markdown via chatgpt_md_converter
    """
    return _convert_to_html(text)


def split_message(text: str, max_length: int = 4096) -> list[str]:
    """Split text into Telegram-compatible HTML chunks.

    Drop-in replacement for telegram_sender.split_message().
    IMPORTANT: Always converts to HTML first, then checks length and splits.
    Raw markdown length can differ greatly from HTML length (e.g. bold/code
    tags, expandable quotes), so length must be checked after conversion.
    """
    html_text = _convert_to_html(text)

    if len(html_text) <= max_length:
        return [html_text]

    return split_html_for_telegram(
        html_text, max_length=max_length, trim_empty_leading_lines=True
    )


def _convert_to_html(text: str) -> str:
    """Convert Markdown with sentinels to Telegram HTML.

    Internal helper that handles the full conversion pipeline.
    """
    if not text:
        return text

    # 1. Preprocess: fix nested backticks in code blocks
    text = _preprocess_nested_backticks(text)

    # 2. Convert expandable quote sentinels to HTML
    text = _convert_expandable_quotes(text)

    # 3. Convert rest of markdown to HTML
    return telegram_format(text)


def strip_sentinels(text: str) -> str:
    """Remove expandable quote sentinels for plain text fallback.

    Used when HTML/MarkdownV2 parsing fails and we need to send plain text.
    """
    return text.replace(EXPANDABLE_QUOTE_START, "").replace(EXPANDABLE_QUOTE_END, "")
