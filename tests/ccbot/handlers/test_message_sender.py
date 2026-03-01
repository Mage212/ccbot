"""Tests for message_sender formatting and fallback behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ccbot.entities_converter import RenderedMessage
from ccbot.handlers import message_sender
from ccbot.handlers.message_sender import (
    _to_plain_text_fallback,
    safe_edit,
    send_with_fallback,
)


def test_plain_fallback_strips_rendered_html_tags():
    text = "<b>Bash</b>(git add src/)\n<blockquote expandable>line</blockquote>"
    got = _to_plain_text_fallback(text)
    assert "<b>" not in got
    assert "</blockquote>" not in got
    assert "Bash(git add src/)" in got


@pytest.mark.asyncio
async def test_send_with_fallback_entities_mode_sends_without_parse_mode(monkeypatch):
    bot = AsyncMock()
    sent = MagicMock()
    sent.message_id = 111
    bot.send_message.return_value = sent

    monkeypatch.setattr(message_sender.config, "use_entities_converter", True)

    result = await send_with_fallback(bot, 123, "**bold**")

    assert result is sent
    bot.send_message.assert_called_once()
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == 123
    assert kwargs.get("parse_mode") is None
    assert "entities" in kwargs


def test_is_already_html_ignores_tags_inside_fenced_code():
    text = "```python\nprint('<code>')\n```\n\n**bold**"
    assert message_sender._is_already_html(text) is False


def test_is_already_html_detects_real_html_markup():
    assert message_sender._is_already_html("<b>bold</b>") is True


@pytest.mark.asyncio
async def test_send_with_fallback_entities_mode_plain_fallback_on_render_error(monkeypatch):
    bot = AsyncMock()
    sent = MagicMock()
    sent.message_id = 222
    bot.send_message.return_value = sent

    monkeypatch.setattr(message_sender.config, "use_entities_converter", True)

    def _boom(_: str):
        raise RuntimeError("boom")

    monkeypatch.setattr(message_sender, "render_markdown_to_entities", _boom)

    result = await send_with_fallback(bot, 123, "**bold**")

    assert result is sent
    bot.send_message.assert_called_once()
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["text"] == "**bold**"
    assert kwargs.get("entities") is None


@pytest.mark.asyncio
async def test_send_with_fallback_entities_mode_fallbacks_failed_chunk_only(monkeypatch):
    bot = AsyncMock()
    first = MagicMock()
    first.message_id = 1
    second = MagicMock()
    second.message_id = 2

    calls: list[dict] = []

    async def _send_message(*, chat_id, text, **kwargs):
        calls.append({"chat_id": chat_id, "text": text, **kwargs})
        if len(calls) == 1:
            return first
        if len(calls) == 2:
            raise RuntimeError("can't parse entities")
        return second

    bot.send_message.side_effect = _send_message

    monkeypatch.setattr(message_sender.config, "use_entities_converter", True)
    monkeypatch.setattr(
        message_sender,
        "render_markdown_to_entities",
        lambda _: RenderedMessage(
            text="chunk-1 chunk-2",
            entities=[],
        ),
    )
    monkeypatch.setattr(
        message_sender,
        "split_text_and_entities",
        lambda *_args, **_kwargs: [
            ("chunk-1", [object()]),
            ("chunk-2", [object()]),
        ],
    )

    result = await send_with_fallback(bot, 123, "ignored")

    assert result is first
    assert len(calls) == 3
    assert calls[0]["text"] == "chunk-1"
    assert calls[0].get("entities")
    assert calls[1]["text"] == "chunk-2"
    assert calls[1].get("entities")
    assert calls[2]["text"] == "chunk-2"
    assert calls[2].get("entities") is None


@pytest.mark.asyncio
async def test_safe_edit_entities_mode_uses_entities(monkeypatch):
    target = AsyncMock()
    monkeypatch.setattr(message_sender.config, "use_entities_converter", True)

    await safe_edit(target, "**bold**")

    target.edit_message_text.assert_called_once()
    args = target.edit_message_text.call_args.args
    kwargs = target.edit_message_text.call_args.kwargs
    assert args
    assert isinstance(args[0], str)
    assert kwargs.get("parse_mode") is None
    assert "entities" in kwargs
