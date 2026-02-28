"""Tests for status_polling — queue-driven interactive UI probing.

Simulates the user workflow: /model is sent to Claude Code, the Settings
model picker renders in the terminal, and the status poller detects it
on its next 1s tick.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.status_polling import update_status_message


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_interactive_state():
    """Ensure interactive state is clean before and after each test."""
    from ccbot.handlers.interactive_ui import _interactive_mode, _interactive_msgs

    _interactive_mode.clear()
    _interactive_msgs.clear()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestStatusPollerSettingsDetection:
    """Simulate the status poller detecting a Settings UI in the terminal.

    This is the actual code path for /model: no JSONL tool_use entry exists,
    so the status poller (update_status_message) is the only detector.
    """

    @pytest.mark.asyncio
    async def test_settings_ui_detected_and_keyboard_sent(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Poller captures Settings pane → enqueue_interactive_probe is called."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.enqueue_interactive_probe",
                new_callable=AsyncMock,
            ) as mock_probe,
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ) as mock_status,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_probe.assert_called_once_with(
                mock_bot, 1, window_id, thread_id=42, source="poller"
            )
            mock_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_normal_pane_no_interactive_ui(self, mock_bot: AsyncMock):
        """Normal pane text → no probe enqueue, status check runs."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        normal_pane = (
            "some output\n"
            "✻ Reading file\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  [Opus 4.6] Context: 50%\n"
        )

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.enqueue_interactive_probe",
                new_callable=AsyncMock,
            ) as mock_probe,
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ) as mock_status,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=normal_pane)

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_probe.assert_not_called()
            mock_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_stale_interactive_msg_triggers_probe_for_clear(
        self, mock_bot: AsyncMock
    ):
        """No UI in pane but tracked interactive message exists → enqueue probe."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        pane_without_ui = "normal output\nno prompt\n"

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.get_interactive_msg_id",
                return_value=777,
            ),
            patch(
                "ccbot.handlers.status_polling.enqueue_interactive_probe",
                new_callable=AsyncMock,
            ) as mock_probe,
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ) as mock_status,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=pane_without_ui)

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_probe.assert_called_once_with(
                mock_bot, 1, window_id, thread_id=42, source="poller"
            )
            mock_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_historical_prompt_does_not_block_statusline(
        self, mock_bot: AsyncMock
    ):
        """Old prompt text in scrollback must not suppress live statusline updates."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        pane_with_old_prompt_and_status = (
            "  Do you want to proceed?\n"
            "   ❯ 1. Yes\n"
            "     2. Yes, and don’t ask again for: uv run mypy src/ --ignore-missing-imports 2>&1\n"
            "     3. No\n"
            "  Esc to cancel · Tab to amend\n"
            "\n"
            "✻ Reading file src/main.py\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  [Opus 4.6] Context: 50%\n"
        )

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.enqueue_interactive_probe",
                new_callable=AsyncMock,
            ) as mock_probe,
            patch(
                "ccbot.handlers.status_polling.enqueue_interactive_clear",
                new_callable=AsyncMock,
            ) as mock_clear,
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ) as mock_status,
            patch(
                "ccbot.handlers.status_polling.get_interactive_msg_id",
                return_value=None,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(
                return_value=pane_with_old_prompt_and_status
            )

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_probe.assert_not_called()
            mock_clear.assert_not_called()
            mock_status.assert_called_once_with(
                mock_bot,
                1,
                window_id,
                "Reading file src/main.py",
                thread_id=42,
            )
