"""Tests for message_queue — interactive UI detection after tool_use.

Tests the race condition fix: interactive UI must appear AFTER tool_use
message in Telegram, not before.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.message_queue import (
    MessageTask,
    _process_content_task,
    _tool_msg_ids,
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
    """Clear message queue state before and after each test."""
    _tool_msg_ids.clear()
    yield
    _tool_msg_ids.clear()


@pytest.fixture
def _clear_interactive_state():
    """Ensure interactive state is clean before and after each test."""
    from ccbot.handlers.interactive_ui import _interactive_mode, _interactive_msgs

    _interactive_mode.clear()
    _interactive_msgs.clear()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()


@pytest.mark.usefixtures("_clear_queue_state", "_clear_interactive_state")
class TestInteractiveUIDetectionAfterToolUse:
    """Test that interactive UI is sent AFTER tool_use message.

    This tests the race condition fix in _process_content_task:
    - tool_use message is sent first
    - Then capture_pane checks for interactive UI
    - If UI found, it's sent immediately after tool_use
    """

    @pytest.mark.asyncio
    async def test_tool_use_with_ui_sends_ui_after_message(
        self, mock_bot: AsyncMock, sample_pane_permission: str
    ):
        """tool_use + interactive UI → message sent, then UI sent."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        task = MessageTask(
            task_type="content",
            window_id=window_id,
            parts=["⚙️ Bash\n```\nls -la\n```"],
            tool_use_id="tool_123",
            content_type="tool_use",
            thread_id=42,
        )

        with (
            patch(
                "ccbot.handlers.message_queue.tmux_manager"
            ) as mock_tmux,
            patch(
                "ccbot.handlers.message_queue.session_manager"
            ) as mock_sm,
            patch(
                "ccbot.handlers.message_queue.handle_interactive_ui"
            ) as mock_handle_ui,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback"
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._send_task_images"
            ) as mock_send_images,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_permission)
            mock_sm.resolve_chat_id.return_value = 100
            mock_send.return_value = sent_msg = MagicMock(message_id=999)
            mock_handle_ui.return_value = True

            await _process_content_task(mock_bot, user_id=1, task=task)

            # 1. tool_use message sent first
            mock_send.assert_called_once()

            # 2. UI detection happened (capture_pane called)
            mock_tmux.capture_pane.assert_called_once()

            # 3. handle_interactive_ui called after message
            mock_handle_ui.assert_called_once_with(
                mock_bot, 1, window_id, 42
            )

            # 4. Images sent (early return path)
            mock_send_images.assert_called_once()

    @pytest.mark.asyncio
    async def test_tool_use_without_ui_no_ui_call(
        self, mock_bot: AsyncMock, sample_pane_no_ui: str
    ):
        """tool_use without interactive UI → no handle_interactive_ui call."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        task = MessageTask(
            task_type="content",
            window_id=window_id,
            parts=["⚙️ Bash\n```\nls -la\n```"],
            tool_use_id="tool_123",
            content_type="tool_use",
            thread_id=42,
        )

        with (
            patch(
                "ccbot.handlers.message_queue.tmux_manager"
            ) as mock_tmux,
            patch(
                "ccbot.handlers.message_queue.session_manager"
            ) as mock_sm,
            patch(
                "ccbot.handlers.message_queue.handle_interactive_ui"
            ) as mock_handle_ui,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback"
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status"
            ) as mock_status,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_no_ui)
            mock_sm.resolve_chat_id.return_value = 100
            mock_send.return_value = MagicMock(message_id=999)

            await _process_content_task(mock_bot, user_id=1, task=task)

            # UI detection attempted but no UI found
            mock_tmux.capture_pane.assert_called_once()

            # handle_interactive_ui NOT called (no UI)
            mock_handle_ui.assert_not_called()

            # Status check happens (normal flow)
            mock_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_tool_use_skips_ui_detection(
        self, mock_bot: AsyncMock, sample_pane_permission: str
    ):
        """Non-tool_use content → no UI detection at all."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        task = MessageTask(
            task_type="content",
            window_id=window_id,
            parts=["Hello, I'm Claude!"],
            content_type="text",  # NOT tool_use
            thread_id=42,
        )

        with (
            patch(
                "ccbot.handlers.message_queue.tmux_manager"
            ) as mock_tmux,
            patch(
                "ccbot.handlers.message_queue.session_manager"
            ) as mock_sm,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback"
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status"
            ) as mock_status,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_sm.resolve_chat_id.return_value = 100
            mock_send.return_value = MagicMock(message_id=999)

            await _process_content_task(mock_bot, user_id=1, task=task)

            # UI detection NOT attempted for non-tool_use
            mock_tmux.capture_pane.assert_not_called()

            # Status check happens (normal flow)
            mock_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_tool_use_id_recorded_before_ui_check(
        self, mock_bot: AsyncMock, sample_pane_permission: str
    ):
        """tool_use_id must be recorded BEFORE UI detection."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        tool_use_id = "tool_abc123"
        task = MessageTask(
            task_type="content",
            window_id=window_id,
            parts=["⚙️ Bash\n```\nls\n```"],
            tool_use_id=tool_use_id,
            content_type="tool_use",
            thread_id=42,
        )

        # Track order of operations
        call_order = []

        def record_capture(*args, **kwargs):
            call_order.append("capture_pane")
            # At this point, tool_use_id MUST be recorded
            assert ("tool_abc123", 1, 42) in _tool_msg_ids
            return sample_pane_permission

        with (
            patch(
                "ccbot.handlers.message_queue.tmux_manager"
            ) as mock_tmux,
            patch(
                "ccbot.handlers.message_queue.session_manager"
            ) as mock_sm,
            patch(
                "ccbot.handlers.message_queue.handle_interactive_ui"
            ) as mock_handle_ui,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback"
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._send_task_images"
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(side_effect=record_capture)
            mock_sm.resolve_chat_id.return_value = 100
            mock_send.return_value = MagicMock(message_id=999)
            mock_handle_ui.return_value = True

            await _process_content_task(mock_bot, user_id=1, task=task)

            # Verify capture_pane was called (our check ran)
            assert "capture_pane" in call_order

    @pytest.mark.asyncio
    async def test_window_not_found_skips_ui_detection(
        self, mock_bot: AsyncMock
    ):
        """If window not found, skip UI detection gracefully."""
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
            patch(
                "ccbot.handlers.message_queue.tmux_manager"
            ) as mock_tmux,
            patch(
                "ccbot.handlers.message_queue.session_manager"
            ) as mock_sm,
            patch(
                "ccbot.handlers.message_queue.handle_interactive_ui"
            ) as mock_handle_ui,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback"
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status"
            ) as mock_status,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)  # Window gone
            mock_sm.resolve_chat_id.return_value = 100
            mock_send.return_value = MagicMock(message_id=999)

            await _process_content_task(mock_bot, user_id=1, task=task)

            # No crash, no UI call, status still happens
            mock_handle_ui.assert_not_called()
            mock_status.assert_called_once()


@pytest.mark.usefixtures("_clear_queue_state", "_clear_interactive_state")
class TestToolUseMessageOrder:
    """Test message ordering guarantees."""

    @pytest.mark.asyncio
    async def test_ui_return_prevents_status_send(
        self, mock_bot: AsyncMock, sample_pane_permission: str
    ):
        """When UI is sent, return early to skip status send."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        task = MessageTask(
            task_type="content",
            window_id=window_id,
            parts=["⚙️ Bash\n```\nls\n```"],
            tool_use_id="tool_123",
            content_type="tool_use",
            thread_id=42,
        )

        with (
            patch(
                "ccbot.handlers.message_queue.tmux_manager"
            ) as mock_tmux,
            patch(
                "ccbot.handlers.message_queue.session_manager"
            ) as mock_sm,
            patch(
                "ccbot.handlers.message_queue.handle_interactive_ui"
            ) as mock_handle_ui,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback"
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status"
            ) as mock_status,
            patch(
                "ccbot.handlers.message_queue._send_task_images"
            ) as mock_send_images,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_permission)
            mock_sm.resolve_chat_id.return_value = 100
            mock_send.return_value = MagicMock(message_id=999)
            mock_handle_ui.return_value = True

            await _process_content_task(mock_bot, user_id=1, task=task)

            # Status NOT called when UI is sent (early return)
            mock_status.assert_not_called()

            # Images called once (in early return path)
            mock_send_images.assert_called_once()
