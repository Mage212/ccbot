"""Tests for interactive_ui — handle_interactive_ui and keyboard layout."""

from unittest.mock import AsyncMock, patch

import pytest

from ccbot.handlers.interactive_ui import (
    _build_interactive_keyboard,
    handle_interactive_ui,
)
from ccbot.handlers.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
)


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
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
class TestHandleInteractiveUI:
    @pytest.mark.asyncio
    async def test_handle_ui_enqueues_probe(self, mock_bot: AsyncMock):
        """Facade should enqueue pane probe instead of sending directly."""
        window_id = "@5"

        with patch(
            "ccbot.handlers.message_queue.enqueue_pane_probe", new_callable=AsyncMock
        ) as mock_enqueue:
            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_enqueue.assert_called_once_with(
                bot=mock_bot,
                user_id=1,
                window_id=window_id,
                thread_id=42,
                source="legacy:handle_interactive_ui",
                allow_status=True,
            )
            assert result is True
        mock_bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_empty_window_returns_false(self, mock_bot: AsyncMock):
        result = await handle_interactive_ui(
            mock_bot, user_id=1, window_id="", thread_id=42
        )

        assert result is False
        mock_bot.send_message.assert_not_called()


class TestKeyboardLayoutForSettings:
    def test_settings_keyboard_includes_all_nav_keys(self):
        """Settings keyboard includes Tab, arrows (not vertical_only), Space, Esc, Enter."""
        keyboard = _build_interactive_keyboard("@5", ui_name="Settings")
        # Flatten callback data values and keep only strings for strict typing.
        all_cb_data = [
            cb
            for row in keyboard.inline_keyboard
            for btn in row
            for cb in [btn.callback_data]
            if isinstance(cb, str)
        ]
        assert any(CB_ASK_TAB in d for d in all_cb_data if d)
        assert any(CB_ASK_SPACE in d for d in all_cb_data if d)
        assert any(CB_ASK_UP in d for d in all_cb_data if d)
        assert any(CB_ASK_DOWN in d for d in all_cb_data if d)
        assert any(CB_ASK_LEFT in d for d in all_cb_data if d)
        assert any(CB_ASK_RIGHT in d for d in all_cb_data if d)
        assert any(CB_ASK_ESC in d for d in all_cb_data if d)
        assert any(CB_ASK_ENTER in d for d in all_cb_data if d)
