"""Safe message sending helpers.

Provides utility functions for sending Telegram messages with automatic
format conversion and fallback to plain text on failure.

Default mode uses HTML formatting via chatgpt-md-converter.
Optional mode (CCBOT_USE_ENTITIES_CONVERTER=true) uses text + entities.
"""

import io
import logging
import re
from html import unescape
from typing import Any

from telegram import Bot, InputMediaPhoto, LinkPreviewOptions, Message, MessageEntity
from telegram.error import BadRequest, RetryAfter

from ..config import config
from ..entities_converter import (
    render_markdown_to_entities,
    split_plain_text,
    split_text_and_entities,
    strip_sentinels,
)
from ..html_converter import convert_markdown

logger = logging.getLogger(__name__)

_RE_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_OPENERS = (
    "<pre",
    "<code",
    "<b>",
    "<i>",
    "<a ",
    "<blockquote",
    "<u>",
    "<s>",
)

# Legacy parse mode (used only when entities converter is disabled)
PARSE_MODE = "HTML"

# Disable link previews in all messages to reduce visual noise
NO_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)


def _is_entities_mode() -> bool:
    return config.use_entities_converter


class _PartialDeliveryError(Exception):
    """Raised when at least one chunk is sent but delivery later fails."""

    def __init__(self, first_message: Message | None):
        super().__init__("partial delivery")
        self.first_message = first_message


def _count_backticks(text: str, start: int) -> int:
    i = start
    while i < len(text) and text[i] == "`":
        i += 1
    return i - start


def _is_already_html(text: str) -> bool:
    """Check if text already contains Telegram HTML formatting.

    Ignores HTML-like literals inside markdown code spans/fenced blocks.
    """
    i = 0
    n = len(text)
    line_start = True
    in_fenced_code = False
    inline_delim_len = 0

    while i < n:
        ch = text[i]

        if line_start and ch == "`":
            tick_count = _count_backticks(text, i)
            if tick_count >= 3:
                in_fenced_code = not in_fenced_code
                i += tick_count
                line_start = False
                continue

        if not in_fenced_code and ch == "`":
            tick_count = _count_backticks(text, i)
            if inline_delim_len == 0:
                inline_delim_len = tick_count
            elif inline_delim_len == tick_count:
                inline_delim_len = 0
            i += tick_count
            line_start = False
            continue

        if not in_fenced_code and inline_delim_len == 0 and ch == "<":
            lower_tail = text[i : i + 20].lower()
            if any(lower_tail.startswith(opener) for opener in _HTML_OPENERS):
                return True

        i += 1
        line_start = ch == "\n"

    return False


def _ensure_html(text: str) -> str:
    """Convert to HTML only if not already converted."""
    if _is_already_html(text):
        return text
    return convert_markdown(text)


def _to_plain_text_fallback(text: str) -> str:
    """Build safe plain-text fallback from markdown or pre-rendered HTML."""
    plain = strip_sentinels(text)
    if not _is_already_html(plain):
        return plain
    # Preserve minimal structure before stripping tags.
    plain = re.sub(r"<br\s*/?>", "\n", plain, flags=re.IGNORECASE)
    plain = re.sub(r"</p\s*>", "\n\n", plain, flags=re.IGNORECASE)
    plain = re.sub(r"<li\s*>", "- ", plain, flags=re.IGNORECASE)
    plain = re.sub(r"</li\s*>", "\n", plain, flags=re.IGNORECASE)
    plain = _RE_HTML_TAG.sub("", plain)
    return unescape(plain).strip()


async def _send_plain_with_chunks(
    bot: Bot,
    chat_id: int,
    text: str,
    **kwargs: Any,
) -> Message | None:
    """Send plain text in chunks, return the first message."""
    first: Message | None = None
    for chunk in split_plain_text(_to_plain_text_fallback(text)):
        sent = await bot.send_message(chat_id=chat_id, text=chunk, **kwargs)
        if first is None:
            first = sent
    return first


async def _send_entities_with_chunks(
    bot: Bot,
    chat_id: int,
    text: str,
    **kwargs: Any,
) -> Message | None:
    """Render markdown to entities, split, and send all chunks."""
    rendered = render_markdown_to_entities(text)
    chunks = split_text_and_entities(rendered.text, rendered.entities, max_chars=4000)

    first: Message | None = None
    for chunk_text, chunk_entities in chunks:
        try:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=chunk_text,
                entities=chunk_entities or None,
                **kwargs,
            )
        except RetryAfter:
            raise
        except Exception as chunk_error:
            logger.debug(
                "Entities chunk send failed, falling back to plain chunk: %s",
                chunk_error,
            )
            try:
                sent = await bot.send_message(
                    chat_id=chat_id,
                    text=chunk_text,
                    **kwargs,
                )
            except RetryAfter:
                raise
            except Exception as fallback_error:
                if first is not None:
                    logger.error(
                        "Chunk fallback failed after partial delivery (chat_id=%d): %s",
                        chat_id,
                        fallback_error,
                    )
                    raise _PartialDeliveryError(first) from fallback_error
                raise
        if first is None:
            first = sent
    return first


async def _reply_entities_with_chunks(
    message: Message,
    text: str,
    **kwargs: Any,
) -> Message:
    """Render markdown to entities, split, and reply with all chunks."""
    rendered = render_markdown_to_entities(text)
    chunks = split_text_and_entities(rendered.text, rendered.entities, max_chars=4000)

    first: Message | None = None
    for chunk_text, chunk_entities in chunks:
        try:
            sent = await message.reply_text(
                chunk_text,
                entities=chunk_entities or None,
                **kwargs,
            )
        except RetryAfter:
            raise
        except Exception as chunk_error:
            logger.debug(
                "Entities chunk reply failed, falling back to plain chunk: %s",
                chunk_error,
            )
            try:
                sent = await message.reply_text(
                    chunk_text,
                    **kwargs,
                )
            except RetryAfter:
                raise
            except Exception as fallback_error:
                if first is not None:
                    logger.error(
                        "Chunk reply fallback failed after partial delivery: %s",
                        fallback_error,
                    )
                    raise _PartialDeliveryError(first) from fallback_error
                raise
        if first is None:
            first = sent

    if first is None:
        first = await message.reply_text("", **kwargs)
    return first


def _render_single_entities_payload(text: str) -> tuple[str, list[MessageEntity] | None]:
    rendered = render_markdown_to_entities(text)
    return rendered.text, (rendered.entities or None)


async def edit_with_fallback(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    propagate_not_modified: bool = False,
    **kwargs: Any,
) -> None:
    """Edit bot message with formatting, falling back to plain text.

    When ``propagate_not_modified`` is True, a Telegram "message is not modified"
    BadRequest is re-raised so callers can treat it as success explicitly.
    """
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        if _is_entities_mode():
            rendered_text, rendered_entities = _render_single_entities_payload(text)
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=rendered_text,
                entities=rendered_entities,
                **kwargs,
            )
        else:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=_ensure_html(text),
                parse_mode=PARSE_MODE,
                **kwargs,
            )
    except RetryAfter:
        raise
    except BadRequest as e:
        if propagate_not_modified and "message is not modified" in str(e).lower():
            raise
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=_to_plain_text_fallback(text),
            **kwargs,
        )
    except Exception:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=_to_plain_text_fallback(text),
            **kwargs,
        )


async def send_with_fallback(
    bot: Bot,
    chat_id: int,
    text: str,
    **kwargs: Any,
) -> Message | None:
    """Send formatted message with fallback to plain text.

    Returns the first sent Message on success (or None on total failure).
    """
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        if _is_entities_mode():
            return await _send_entities_with_chunks(bot, chat_id, text, **kwargs)
        return await bot.send_message(
            chat_id=chat_id,
            text=_ensure_html(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except _PartialDeliveryError as partial:
        return partial.first_message
    except Exception as e:
        logger.debug("Formatted send failed, falling back to plain text: %s", e)
        try:
            return await _send_plain_with_chunks(bot, chat_id, text, **kwargs)
        except RetryAfter:
            raise
        except Exception as fallback_error:
            logger.error("Failed to send message to %d: %s", chat_id, fallback_error)
            return None


async def send_photo(
    bot: Bot,
    chat_id: int,
    image_data: list[tuple[str, bytes]],
    **kwargs: Any,
) -> None:
    """Send photo(s) to chat. Sends as media group if multiple images."""
    if not image_data:
        return
    try:
        if len(image_data) == 1:
            _media_type, raw_bytes = image_data[0]
            await bot.send_photo(
                chat_id=chat_id,
                photo=io.BytesIO(raw_bytes),
                **kwargs,
            )
        else:
            media = [
                InputMediaPhoto(media=io.BytesIO(raw_bytes))
                for _media_type, raw_bytes in image_data
            ]
            await bot.send_media_group(
                chat_id=chat_id,
                media=media,
                **kwargs,
            )
    except RetryAfter:
        raise
    except Exception as e:
        logger.error("Failed to send photo to %d: %s", chat_id, e)


async def safe_reply(message: Message, text: str, **kwargs: Any) -> Message:
    """Reply with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        if _is_entities_mode():
            return await _reply_entities_with_chunks(message, text, **kwargs)
        return await message.reply_text(
            _ensure_html(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except _PartialDeliveryError as partial:
        if partial.first_message is None:
            raise
        return partial.first_message
    except Exception:
        try:
            first: Message | None = None
            for chunk in split_plain_text(_to_plain_text_fallback(text)):
                sent = await message.reply_text(chunk, **kwargs)
                if first is None:
                    first = sent
            if first is None:
                first = await message.reply_text("", **kwargs)
            return first
        except RetryAfter:
            raise
        except Exception as e:
            logger.error("Failed to reply: %s", e)
            raise


async def safe_edit(target: Any, text: str, **kwargs: Any) -> None:
    """Edit message with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        if _is_entities_mode():
            rendered_text, rendered_entities = _render_single_entities_payload(text)
            await target.edit_message_text(
                rendered_text,
                entities=rendered_entities,
                **kwargs,
            )
        else:
            await target.edit_message_text(
                _ensure_html(text),
                parse_mode=PARSE_MODE,
                **kwargs,
            )
    except RetryAfter:
        raise
    except Exception:
        try:
            await target.edit_message_text(_to_plain_text_fallback(text), **kwargs)
        except RetryAfter:
            raise
        except Exception as e:
            logger.error("Failed to edit message: %s", e)


async def safe_send(
    bot: Bot,
    chat_id: int,
    text: str,
    message_thread_id: int | None = None,
    **kwargs: Any,
) -> None:
    """Send message with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    if message_thread_id is not None:
        kwargs.setdefault("message_thread_id", message_thread_id)

    try:
        if _is_entities_mode():
            await _send_entities_with_chunks(bot, chat_id, text, **kwargs)
            return
        await bot.send_message(
            chat_id=chat_id,
            text=_ensure_html(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except _PartialDeliveryError:
        return
    except Exception:
        try:
            await _send_plain_with_chunks(bot, chat_id, text, **kwargs)
        except RetryAfter:
            raise
        except Exception as e:
            logger.error("Failed to send message to %d: %s", chat_id, e)
