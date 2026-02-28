"""Tests for plain-text fallback behavior in message_sender."""

from ccbot.handlers.message_sender import _to_plain_text_fallback


def test_plain_fallback_strips_rendered_html_tags():
    text = "<b>Bash</b>(git add src/)\n<blockquote expandable>line</blockquote>"
    got = _to_plain_text_fallback(text)
    assert "<b>" not in got
    assert "</blockquote>" not in got
    assert "Bash(git add src/)" in got
