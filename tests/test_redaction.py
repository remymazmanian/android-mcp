"""Sensitive tool arguments never reach the action log or error messages."""

import asyncio
import subprocess

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from android_mcp import adb, server

SECRET = "hunter2 correct-horse $ecret"
SECRET_PARTS = ("hunter2", "correct-horse", "$ecret")


def assert_hidden(*texts: str) -> None:
    for text in texts:
        for part in SECRET_PARTS:
            assert part not in text, f"{part!r} leaked into: {text}"


def test_type_text_logs_only_the_length(device, action_log):
    server.type_text(text=SECRET)

    log = action_log()
    assert f'"text": "[redacted {len(SECRET)} chars]"' in log
    assert "| ok |" in log
    assert_hidden(log)
    assert any(c.startswith("input text ") for c in device.shell_commands), "text should still reach the device"


def test_type_text_error_path_redacts_log_and_message(monkeypatch, action_log):
    """Run the real adb.run against a failing process that echoes its command, like adb does."""
    monkeypatch.setattr(adb, "select_serial", lambda: "emulator-5554")
    monkeypatch.setattr(adb, "adb_binary", lambda: "adb")

    def failing_adb(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, b"", f"error: could not run {cmd[-1]}".encode())

    monkeypatch.setattr(adb.subprocess, "run", failing_adb)

    with pytest.raises(ToolError) as excinfo:
        server.type_text(text=SECRET)

    log = action_log()
    assert "| error |" in log
    assert "[redacted]" in str(excinfo.value)
    assert_hidden(log, str(excinfo.value))


def test_type_text_timeout_message_is_redacted(monkeypatch, action_log):
    monkeypatch.setattr(adb, "select_serial", lambda: "emulator-5554")
    monkeypatch.setattr(adb, "adb_binary", lambda: "adb")

    def hanging_adb(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 30))

    monkeypatch.setattr(adb.subprocess, "run", hanging_adb)

    with pytest.raises(ToolError, match="timed out") as excinfo:
        server.type_text(text=SECRET)
    assert_hidden(action_log(), str(excinfo.value))


def test_type_text_crash_path_withholds_details(device, monkeypatch, action_log):
    def explode(command, **kwargs):
        raise RuntimeError(f"device exploded while running {command}")

    monkeypatch.setattr(adb, "shell", explode)

    with pytest.raises(ToolError, match="details withheld") as excinfo:
        server.type_text(text=SECRET)

    log = action_log()
    assert "| crash |" in log
    assert_hidden(log, str(excinfo.value))


def test_type_text_rejected_non_ascii_is_redacted(device, action_log):
    device.run_replies["shell"] = b"No shell command implementation."

    with pytest.raises(ToolError, match="Nothing was typed"):
        server.type_text(text="pässwörd ☕ hunter2")

    assert not any(c.startswith("input text") for c in device.shell_commands)
    assert "pässwörd" not in action_log()
    assert_hidden(action_log())


def test_registered_mcp_tool_redacts_too(device, action_log):
    """Calls arriving over MCP go through the same redacting wrapper."""

    async def call():
        async with Client(server.mcp) as client:
            return await client.call_tool("type_text", {"text": SECRET})

    result = asyncio.run(call())

    assert not result.is_error
    assert "[redacted" in action_log()
    assert_hidden(action_log())


@pytest.mark.parametrize(
    ("url", "logged"),
    [
        ("https://example.com", "https://example.com"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com/reset/TOKEN123?code=abc#frag", "https://example.com/[redacted 28 chars]"),
        ("https://user:pw@example.com:8443/x", "https://[redacted]@example.com:8443/[redacted 1 chars]"),
        ("market://details?id=com.example", "market://details[redacted 15 chars]"),
        ("tel:5551234", "tel:[redacted 7 chars]"),
        ("no scheme", "[redacted 9 chars]"),
    ],
)
def test_redact_url_secrets(url, logged):
    assert server.redact_url_secrets(url) == logged


def test_open_url_logs_scheme_and_host_only(device, action_log):
    device.shell_replies["am start"] = "Status: ok\nActivity: com.android.chrome/.Main"

    server.open_url(url="https://user:pw@example.com/reset/TOKEN123?code=abc#frag")

    log = action_log()
    assert '"url": "https://[redacted]@example.com/[redacted' in log
    for part in ("TOKEN123", "code=abc", "user:pw", "#frag"):
        assert part not in log


def test_open_url_error_message_is_scrubbed(device, action_log):
    url = "https://example.com/reset/TOKEN123"
    device.shell_replies["am start"] = f"Error: Activity not started, unable to resolve Intent {{ dat={url} }}"

    with pytest.raises(ToolError, match="Could not open https://example.com/") as excinfo:
        server.open_url(url=url)

    assert "TOKEN123" not in action_log()
    assert "TOKEN123" not in str(excinfo.value)


def test_scrub_keeps_short_secrets_from_shredding_messages():
    assert adb.scrub("adb shell input text a", ["a"]) == "adb shell input text [redacted]"
    assert adb.scrub("pass=longsecret!", ["longsecret"]) == "pass=[redacted]!"
