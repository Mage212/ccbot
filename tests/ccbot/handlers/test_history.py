"""Tests for history pagination and entities-mode delivery."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import MessageEntity

from ccbot.entities_converter import RenderedMessage
from ccbot.handlers import history


@pytest.mark.asyncio
async def test_send_history_entities_mode_splits_after_render(monkeypatch):
    monkeypatch.setattr(history.config, "use_entities_converter", True)
    monkeypatch.setattr(history.config, "show_user_messages", True)
    monkeypatch.setattr(history.session_manager, "get_display_name", lambda _: "demo")

    async def _get_recent_messages(*_args, **_kwargs):
        return (
            [
                {
                    "text": "```python\nprint('x')\n```\n\n[1/3]",
                    "role": "assistant",
                    "content_type": "text",
                    "timestamp": "2026-01-01T12:00:00.000Z",
                }
            ],
            1,
        )

    monkeypatch.setattr(history.session_manager, "get_recent_messages", _get_recent_messages)
    monkeypatch.setattr(
        history,
        "split_html_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("split_html_message should not be used in entities mode")
        ),
    )

    pre_entity = MessageEntity(
        type=MessageEntity.PRE,
        offset=0,
        length=8,
        language="python",
    )
    render_calls: list[str] = []
    split_calls: list[tuple[str, int]] = []

    def _render(text: str):
        render_calls.append(text)
        return RenderedMessage(text="print('x')\n\n[1/3]", entities=[pre_entity])

    def _split(text: str, entities: list[MessageEntity], max_chars: int = 4096):
        split_calls.append((text, max_chars))
        return [
            ("print('x')\n", [pre_entity]),
            ("[1/3]", []),
        ]

    monkeypatch.setattr(history, "render_markdown_to_entities", _render)
    monkeypatch.setattr(history, "split_text_and_entities", _split)

    target = MagicMock()
    target.reply_text = AsyncMock(return_value=MagicMock())

    await history.send_history(target, window_id="@5", offset=0, edit=False)

    assert len(render_calls) == 1
    assert len(split_calls) == 1
    target.reply_text.assert_awaited_once()
    args = target.reply_text.call_args.args
    kwargs = target.reply_text.call_args.kwargs
    assert args[0] == "print('x')\n"
    assert kwargs["entities"] == [pre_entity]


@pytest.mark.asyncio
async def test_send_history_entities_mode_reply_falls_back_to_plain_chunk(monkeypatch):
    monkeypatch.setattr(history.config, "use_entities_converter", True)
    monkeypatch.setattr(history.config, "show_user_messages", True)
    monkeypatch.setattr(history.session_manager, "get_display_name", lambda _: "demo")

    async def _get_recent_messages(*_args, **_kwargs):
        return (
            [
                {
                    "text": "**bold**",
                    "role": "assistant",
                    "content_type": "text",
                    "timestamp": "2026-01-01T12:00:00.000Z",
                }
            ],
            1,
        )

    monkeypatch.setattr(history.session_manager, "get_recent_messages", _get_recent_messages)
    monkeypatch.setattr(
        history,
        "render_markdown_to_entities",
        lambda _text: RenderedMessage(
            text="bold",
            entities=[MessageEntity(type=MessageEntity.BOLD, offset=0, length=4)],
        ),
    )
    monkeypatch.setattr(
        history,
        "split_text_and_entities",
        lambda *_args, **_kwargs: [
            (
                "bold",
                [MessageEntity(type=MessageEntity.BOLD, offset=0, length=4)],
            )
        ],
    )

    target = MagicMock()
    target.reply_text = AsyncMock(side_effect=[RuntimeError("entity error"), MagicMock()])

    await history.send_history(target, window_id="@5", offset=0, edit=False)

    assert target.reply_text.await_count == 2
    first_kwargs = target.reply_text.await_args_list[0].kwargs
    second_kwargs = target.reply_text.await_args_list[1].kwargs
    assert first_kwargs["entities"]
    assert second_kwargs.get("entities") is None
