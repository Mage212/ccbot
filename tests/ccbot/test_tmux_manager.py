"""Unit tests for tmux manager helpers."""

from pathlib import Path

from ccbot.config import config
from ccbot.tmux_manager import TmuxManager


def test_build_claude_launch_command_unsets_env_vars(monkeypatch: object) -> None:
    manager = TmuxManager(session_name="test")
    monkeypatch.setattr(config, "claude_command", "claude")

    cmd = manager._build_claude_launch_command(Path("/tmp/my project"))

    assert cmd.startswith("cd '/tmp/my project' && ")
    assert "unset VIRTUAL_ENV UV_PROJECT UV_WORKING_DIRECTORY && " in cmd
    assert cmd.endswith("claude")


def test_build_claude_launch_command_preserves_custom_claude_command(
    monkeypatch: object,
) -> None:
    manager = TmuxManager(session_name="test")
    monkeypatch.setattr(
        config,
        "claude_command",
        "IS_SANDBOX=1 claude --dangerously-skip-permissions",
    )

    cmd = manager._build_claude_launch_command(Path("/tmp/work"))

    assert "unset VIRTUAL_ENV UV_PROJECT UV_WORKING_DIRECTORY" in cmd
    assert cmd.endswith("IS_SANDBOX=1 claude --dangerously-skip-permissions")
