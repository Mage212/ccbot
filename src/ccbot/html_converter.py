"""HTML-based markdown conversion and splitting using chatgpt_md_converter.

Drop-in replacement for markdown_v2.py using HTML output format instead of MarkdownV2.
Provides convert_markdown() and split_message() functions with compatible signatures.

When CCBOT_USE_HTML_CONVERTER=true, the bot uses HTML parse mode which allows
cleaner formatting and more reliable tag-aware message splitting.
"""

import logging
import re

from chatgpt_md_converter import split_html_for_telegram, telegram_format

from .transcript_parser import TranscriptParser

logger = logging.getLogger(__name__)

# Re-export sentinel markers for compatibility
EXPANDABLE_QUOTE_START = TranscriptParser.EXPANDABLE_QUOTE_START
EXPANDABLE_QUOTE_END = TranscriptParser.EXPANDABLE_QUOTE_END


def _split_table_row(line: str) -> list[str]:
    """Split a table row by pipes, respecting escaped pipes (\\|)."""
    content = line.strip().strip("|")
    cells = re.split(r"(?<!\\)\|", content)
    return [cell.strip().replace("\\|", "|") for cell in cells]


def _convert_markdown_tables(text: str) -> str:
    """Convert markdown tables to card-style key-value format.

    Telegram has no table rendering. This converts each row into a card
    with **Header**: value pairs, separated by horizontal lines — similar
    to how Claude Code renders tables in narrow terminals.

    Skips tables inside code blocks. Uses markdown bold so that
    telegram_format() can process both headers and cell inline markup.
    """
    lines = text.split("\n")
    result: list[str] = []
    i = 0
    in_code_block = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Track code blocks
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            result.append(line)
            i += 1
            continue

        if in_code_block:
            result.append(line)
            i += 1
            continue

        # Check if this looks like a table header row
        if (
            stripped.startswith("|")
            and stripped.endswith("|")
            and "|" in stripped[1:-1]
        ):
            headers = _split_table_row(stripped)

            # Next line must be separator (---|---|---)
            if i + 1 < len(lines):
                sep_line = lines[i + 1].strip()
                if sep_line.startswith("|") and re.match(r"^[\s|:\-]+$", sep_line):
                    i += 2  # Skip header + separator
                    rows: list[list[str]] = []
                    while i < len(lines):
                        data_line = lines[i].strip()
                        if data_line.startswith("|") and data_line.endswith("|"):
                            rows.append(_split_table_row(data_line))
                            i += 1
                        else:
                            break

                    # Build card-style output
                    separator = "────────────"
                    cards: list[str] = []
                    for row in rows:
                        card_lines: list[str] = []
                        for j, header in enumerate(headers):
                            value = row[j] if j < len(row) else ""
                            if value:
                                card_lines.append(f"**{header}**: {value}")
                            else:
                                card_lines.append(f"**{header}**: —")
                        cards.append("\n".join(card_lines))

                    result.append(f"\n{separator}\n".join(cards))
                    continue

        result.append(line)
        i += 1

    return "\n".join(result)


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
    Raw markdown length can differ greatly from HTML length (e.g. table→card
    expansion, bold/code tags), so length must be checked after conversion.
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

    # 2. Convert markdown tables to card-style format (before telegram_format
    #    so that inline markup inside cells gets processed)
    text = _convert_markdown_tables(text)

    # 3. Convert expandable quote sentinels to HTML
    text = _convert_expandable_quotes(text)

    # 4. Convert rest of markdown to HTML
    return telegram_format(text)


def strip_sentinels(text: str) -> str:
    """Remove expandable quote sentinels for plain text fallback.

    Used when HTML/MarkdownV2 parsing fails and we need to send plain text.
    """
    return text.replace(EXPANDABLE_QUOTE_START, "").replace(EXPANDABLE_QUOTE_END, "")
