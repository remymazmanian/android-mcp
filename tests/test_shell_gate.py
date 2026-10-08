"""shell() only runs when ANDROID_MCP_ALLOW_SHELL is exactly "1"."""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from android_mcp import server


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes", " 1"])
def test_shell_is_refused_unless_enabled(device, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ANDROID_MCP_ALLOW_SHELL", raising=False)
    else:
        monkeypatch.setenv("ANDROID_MCP_ALLOW_SHELL", value)

    with pytest.raises(ToolError, match="shell\\(\\) is disabled.*ANDROID_MCP_ALLOW_SHELL=1"):
        server.shell(command="rm -rf /sdcard/Download")

    assert device.run_calls == []
    assert device.shell_commands == []


def test_shell_runs_when_enabled(device, monkeypatch):
    monkeypatch.setenv("ANDROID_MCP_ALLOW_SHELL", "1")
    device.run_replies["shell"] = b"hello"

    result = json.loads(server.shell(command="echo hello"))

    assert result == {"exit_code": 0, "stdout": "hello", "stderr": ""}
    assert device.run_calls == [["shell", "echo hello"]]


def test_shell_command_is_logged_verbatim(device, monkeypatch, action_log):
    """Deliberately not redacted: the audit log must show exactly what the escape hatch ran."""
    monkeypatch.setenv("ANDROID_MCP_ALLOW_SHELL", "1")

    server.shell(command="ls /sdcard/Download")

    assert '"command": "ls /sdcard/Download"' in action_log()
