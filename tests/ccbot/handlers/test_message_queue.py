"""Tests for message_queue interactive UI rendering and ordering."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from ccbot.handlers.message_queue import (
    MessageTask,
    _interactive_probe_pending,
    _interactive_render_state,
    _process_content_task,
    _process_interactive_probe_task,
    _tool_msg_ids,
    enqueue_interactive_probe,
)


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 12345
    bot.send_message.return_value = sent_msg
    bot.edit_message_text.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_queue_state():
    """Clear queue/interactivity state before and after each test."""
    _tool_msg_ids.clear()
    _interactive_render_state.clear()
    _interactive_probe_pending.clear()
    yield
    _tool_msg_ids.clear()
    _interactive_render_state.clear()
    _interactive_probe_pending.clear()


@pytest.fixture
def _clear_interactive_state():
    """Ensure interactive_ui state is clean before and after each test."""
    from ccbot.handlers.interactive_ui import _interactive_mode, _interactive_msgs

    _interactive_mode.clear()
    _interactive_msgs.clear()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()


@pytest.mark.usefixtures("_clear_queue_state", "_clear_interactive_state")
class TestToolUseInteractiveOrdering:
    """Tool-use output must be delivered before probe-driven UI updates."""

    @pytest.mark.asyncio
    async def test_tool_use_with_ui_skips_status_send(
        self, mock_bot: AsyncMock, sample_pane_permission: str
    ):
        window_id = "@5"
        task = MessageTask(
            task_type="content",
            window_id=window_id,
            parts=["⚙️ Bash\n```\nls -la\n```"],
            tool_use_id="tool_123",
            content_type="tool_use",
            thread_id=42,
        )

        with (
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback"
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._process_interactive_probe_task",
                new_callable=AsyncMock,
            ) as mock_probe,
            patch(
                "ccbot.handlers.message_queue._send_task_images"
            ) as mock_send_images,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status"
            ) as mock_status,
        ):
            mock_sm.resolve_chat_id.return_value = 100
            mock_send.return_value = MagicMock(message_id=999)
            mock_probe.return_value = True

            await _process_content_task(mock_bot, user_id=1, task=task)

            mock_send.assert_called_once()
            mock_probe.assert_called_once()
            mock_send_images.assert_called_once()
            mock_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_tool_use_without_ui_sends_status(
        self, mock_bot: AsyncMock, sample_pane_no_ui: str
    ):
        window_id = "@5"
        task = MessageTask(
            task_type="content",
            window_id=window_id,
            parts=["⚙️ Bash\n```\nls\n```"],
            tool_use_id="tool_123",
            content_type="tool_use",
            thread_id=42,
        )

        with (
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback"
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._process_interactive_probe_task",
                new_callable=AsyncMock,
            ) as mock_probe,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status"
            ) as mock_status,
        ):
            mock_sm.resolve_chat_id.return_value = 100
            mock_send.return_value = MagicMock(message_id=999)
            mock_probe.return_value = False

            await _process_content_task(mock_bot, user_id=1, task=task)

            mock_probe.assert_called_once()
            mock_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_tool_use_skips_probe(self, mock_bot: AsyncMock):
        window_id = "@5"
        task = MessageTask(
            task_type="content",
            window_id=window_id,
            parts=["Hello, I'm Claude!"],
            content_type="text",
            thread_id=42,
        )

        with (
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback"
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._process_interactive_probe_task",
                new_callable=AsyncMock,
            ) as mock_probe,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status"
            ) as mock_status,
        ):
            mock_sm.resolve_chat_id.return_value = 100
            mock_send.return_value = MagicMock(message_id=999)

            await _process_content_task(mock_bot, user_id=1, task=task)

            mock_probe.assert_not_called()
            mock_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_tool_use_id_recorded_before_probe(self, mock_bot: AsyncMock):
        task = MessageTask(
            task_type="content",
            window_id="@5",
            parts=["⚙️ Bash\n```\nls\n```"],
            tool_use_id="tool_abc123",
            content_type="tool_use",
            thread_id=42,
        )

        async def assert_recorded(*args, **kwargs):
            assert ("tool_abc123", 1, 42) in _tool_msg_ids
            return False

        with (
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback"
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._process_interactive_probe_task",
                side_effect=assert_recorded,
            ) as mock_probe,
        ):
            mock_sm.resolve_chat_id.return_value = 100
            mock_send.return_value = MagicMock(message_id=999)

            await _process_content_task(mock_bot, user_id=1, task=task)

            mock_probe.assert_called_once()


@pytest.mark.usefixtures("_clear_queue_state", "_clear_interactive_state")
class TestInteractiveProbeRendering:
    @pytest.mark.asyncio
    async def test_same_fingerprint_no_duplicate_send(
        self, mock_bot: AsyncMock, sample_pane_permission: str
    ):
        task = MessageTask(
            task_type="interactive_probe",
            window_id="@5",
            thread_id=42,
            source="poller",
        )
        mock_window = MagicMock(window_id="@5")

        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_permission)
            mock_sm.resolve_chat_id.return_value = 100

            await _process_interactive_probe_task(mock_bot, user_id=1, task=task)
            await _process_interactive_probe_task(mock_bot, user_id=1, task=task)
            await _process_interactive_probe_task(mock_bot, user_id=1, task=task)

            mock_bot.send_message.assert_called_once()
            mock_bot.edit_message_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_message_not_modified_badrequest_does_not_send_new(
        self, mock_bot: AsyncMock, sample_pane_permission: str
    ):
        task = MessageTask(
            task_type="interactive_probe",
            window_id="@5",
            thread_id=42,
            source="poller",
        )
        mock_window = MagicMock(window_id="@5")
        _interactive_render_state[(1, 42)] = (999, "@5", "oldfingerprint")

        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_permission)
            mock_sm.resolve_chat_id.return_value = 100
            mock_bot.edit_message_text.side_effect = BadRequest(
                "Message is not modified"
            )

            visible = await _process_interactive_probe_task(
                mock_bot, user_id=1, task=task
            )

            assert visible is True
            mock_bot.send_message.assert_not_called()
            mock_bot.edit_message_text.assert_called_once()
            assert _interactive_render_state[(1, 42)][0] == 999

    @pytest.mark.asyncio
    async def test_changed_fingerprint_edits_existing_message(
        self, mock_bot: AsyncMock, sample_pane_permission: str
    ):
        task = MessageTask(
            task_type="interactive_probe",
            window_id="@5",
            thread_id=42,
            source="poller",
        )
        mock_window = MagicMock(window_id="@5")
        _interactive_render_state[(1, 42)] = (999, "@5", "oldfingerprint")

        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_permission)
            mock_sm.resolve_chat_id.return_value = 100

            visible = await _process_interactive_probe_task(
                mock_bot, user_id=1, task=task
            )

            assert visible is True
            mock_bot.edit_message_text.assert_called_once()
            mock_bot.send_message.assert_not_called()
            assert _interactive_render_state[(1, 42)][0] == 999


@pytest.mark.usefixtures("_clear_queue_state")
class TestInteractiveProbeEnqueue:
    @pytest.mark.asyncio
    async def test_enqueue_interactive_probe_coalesces_same_key(self, mock_bot: AsyncMock):
        queue = MagicMock()
        queue.put_nowait = MagicMock()

        with patch(
            "ccbot.handlers.message_queue.get_or_create_queue", return_value=queue
        ):
            await enqueue_interactive_probe(
                mock_bot,
                user_id=1,
                window_id="@5",
                thread_id=42,
                source="poller",
            )
            await enqueue_interactive_probe(
                mock_bot,
                user_id=1,
                window_id="@5",
                thread_id=42,
                source="poller",
            )
            await enqueue_interactive_probe(
                mock_bot,
                user_id=1,
                window_id="@5",
                thread_id=42,
                source="poller",
            )

        queue.put_nowait.assert_called_once()
        assert (1, 42, "@5") in _interactive_probe_pending
