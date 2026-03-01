"""Markdown to Telegram entities conversion utilities.

This module provides a safer conversion pipeline for Telegram messages:
Markdown -> HTML -> text + MessageEntity.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from markdown_it import MarkdownIt
from sulguk import transform_html
from telegram import MessageEntity

from .transcript_parser import TranscriptParser

EXPANDABLE_QUOTE_START = TranscriptParser.EXPANDABLE_QUOTE_START
EXPANDABLE_QUOTE_END = TranscriptParser.EXPANDABLE_QUOTE_END

_MARKDOWN_RENDERER = MarkdownIt("commonmark", {"breaks": True}).enable("strikethrough")

_SPOILER_OPEN = "<tg-spoiler>"
_SPOILER_CLOSE = "</tg-spoiler>"
_HTML_TAG_HINT_RE = re.compile(
    r"<(?:pre|code|b|i|a|blockquote|u|s|tg-spoiler|span|strong|em)\b",
    flags=re.IGNORECASE,
)

_SUPPORTED_ENTITY_TYPES: set[str] = {
    item.value if hasattr(item, "value") else str(item) for item in MessageEntity.ALL_TYPES
}


@dataclass(slots=True)
class RenderedMessage:
    """Rendered Telegram message payload."""

    text: str
    entities: list[MessageEntity]


def strip_sentinels(text: str) -> str:
    """Remove expandable quote sentinels for plain-text fallback."""
    return text.replace(EXPANDABLE_QUOTE_START, "").replace(EXPANDABLE_QUOTE_END, "")


def _replace_expandable_quotes(text: str) -> str:
    text = text.replace(EXPANDABLE_QUOTE_START, "<blockquote expandable>")
    return text.replace(EXPANDABLE_QUOTE_END, "</blockquote>")


def _count_backticks(text: str, start: int) -> int:
    i = start
    while i < len(text) and text[i] == "`":
        i += 1
    return i - start


def _replace_spoilers_outside_code(text: str) -> str:
    """Convert ||spoiler|| to <tg-spoiler>spoiler</tg-spoiler> outside code."""
    out: list[str] = []
    i = 0
    line_start = True
    in_fenced_code = False
    inline_delim_len = 0
    n = len(text)

    while i < n:
        ch = text[i]

        if line_start and ch == "`":
            tick_count = _count_backticks(text, i)
            if tick_count >= 3:
                out.append("`" * tick_count)
                in_fenced_code = not in_fenced_code
                i += tick_count
                line_start = False
                continue

        if not in_fenced_code and ch == "`":
            tick_count = _count_backticks(text, i)
            out.append("`" * tick_count)
            if inline_delim_len == 0:
                inline_delim_len = tick_count
            elif inline_delim_len == tick_count:
                inline_delim_len = 0
            i += tick_count
            line_start = False
            continue

        if (
            not in_fenced_code
            and inline_delim_len == 0
            and ch == "|"
            and i + 1 < n
            and text[i + 1] == "|"
        ):
            close = -1
            j = i + 2
            while j + 1 < n:
                if text[j] == "\n":
                    break
                if text[j] == "|" and text[j + 1] == "|":
                    close = j
                    break
                j += 1
            if close != -1 and close > i + 2:
                out.append(_SPOILER_OPEN)
                out.append(text[i + 2 : close])
                out.append(_SPOILER_CLOSE)
                i = close + 2
                line_start = False
                continue

        out.append(ch)
        i += 1
        line_start = ch == "\n"

    return "".join(out)


def _preprocess_markdown(text: str) -> str:
    text = _replace_expandable_quotes(text)
    return _replace_spoilers_outside_code(text)


def _supported_entity_type(raw_type: object) -> str | None:
    if not isinstance(raw_type, str):
        return None
    if raw_type in _SUPPORTED_ENTITY_TYPES:
        return raw_type
    return None


def _to_message_entities(raw_entities: list[dict[str, object]]) -> list[MessageEntity]:
    result: list[MessageEntity] = []

    for raw in raw_entities:
        etype = _supported_entity_type(raw.get("type"))
        if etype is None:
            continue

        offset = raw.get("offset")
        length = raw.get("length")
        if not isinstance(offset, int) or not isinstance(length, int):
            continue
        if offset < 0 or length <= 0:
            continue

        result.append(
            MessageEntity(
                type=etype,
                offset=offset,
                length=length,
                url=raw.get("url") if isinstance(raw.get("url"), str) else None,
                language=(
                    raw.get("language")
                    if isinstance(raw.get("language"), str)
                    else None
                ),
                custom_emoji_id=(
                    raw.get("custom_emoji_id")
                    if isinstance(raw.get("custom_emoji_id"), str)
                    else None
                ),
            )
        )

    pre_ranges = {
        (entity.offset, entity.length) for entity in result if entity.type == MessageEntity.PRE
    }
    deduped: list[MessageEntity] = []
    seen: set[tuple[object, int, int, str | None, str | None, str | None]] = set()
    for entity in result:
        if entity.type == MessageEntity.CODE and (entity.offset, entity.length) in pre_ranges:
            continue
        key = (
            entity.type,
            entity.offset,
            entity.length,
            entity.url,
            entity.language,
            entity.custom_emoji_id,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entity)

    return sorted(deduped, key=lambda e: (e.offset, e.length))


def render_markdown_to_entities(markdown_text: str) -> RenderedMessage:
    """Render Markdown into Telegram text + entities."""
    if not markdown_text:
        return RenderedMessage(text="", entities=[])

    source_is_html = _HTML_TAG_HINT_RE.search(markdown_text) is not None
    preprocessed = _preprocess_markdown(markdown_text)
    if source_is_html:
        html_content = preprocessed
    else:
        html_content = _MARKDOWN_RENDERER.render(preprocessed)
    transformed = transform_html(html_content, strict=False)
    text = transformed.text or ""
    raw_entities = transformed.entities if isinstance(transformed.entities, list) else []
    entities = _to_message_entities(raw_entities)
    return RenderedMessage(text=text, entities=entities)


def _utf16_prefix_lengths(text: str) -> list[int]:
    prefix = [0]
    total = 0
    for ch in text:
        total += 2 if ord(ch) > 0xFFFF else 1
        prefix.append(total)
    return prefix


def split_text_and_entities(
    text: str,
    entities: list[MessageEntity],
    max_chars: int = 4000,
) -> list[tuple[str, list[MessageEntity]]]:
    """Split message text and clip entities for each chunk."""
    if max_chars <= 0:
        raise ValueError("max_chars must be > 0")
    if len(text) <= max_chars:
        return [(text, entities)]

    prefix = _utf16_prefix_lengths(text)
    chunks: list[tuple[str, list[MessageEntity]]] = []
    start_cp = 0
    n = len(text)

    while start_cp < n:
        end_cp = min(start_cp + max_chars, n)
        if end_cp < n:
            preferred_from = start_cp + max_chars // 2
            newline = text.rfind("\n", preferred_from, end_cp)
            if newline > start_cp:
                end_cp = newline + 1
            else:
                space = text.rfind(" ", preferred_from, end_cp)
                if space > start_cp:
                    end_cp = space + 1
        if end_cp <= start_cp:
            end_cp = min(start_cp + max_chars, n)

        utf16_start = prefix[start_cp]
        utf16_end = prefix[end_cp]

        chunk_entities: list[MessageEntity] = []
        for entity in entities:
            ent_start = entity.offset
            ent_end = entity.offset + entity.length
            if ent_end <= utf16_start or ent_start >= utf16_end:
                continue
            clipped_start = max(ent_start, utf16_start)
            clipped_end = min(ent_end, utf16_end)
            if clipped_end <= clipped_start:
                continue
            chunk_entities.append(
                MessageEntity(
                    type=entity.type,
                    offset=clipped_start - utf16_start,
                    length=clipped_end - clipped_start,
                    url=entity.url,
                    user=entity.user,
                    language=entity.language,
                    custom_emoji_id=entity.custom_emoji_id,
                )
            )

        chunks.append((text[start_cp:end_cp], chunk_entities))
        start_cp = end_cp

    return chunks
