"""Tests for message_sender formatting and fallback behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

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
