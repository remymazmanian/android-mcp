"""type_text turns text into safely quoted `input text` / `input keyevent` commands."""

import re
import shlex

import pytest

from android_mcp import server

SHELL_SPECIAL = "a'b \"c\" $HOME `id` $(reboot) & | ; < > ( ) [ ] { } * ? ~ # ! \\ ^ = + -rf"


def device_input_text(arg: str) -> str:
    """What Android's `input text` types for one argument: each "%s" becomes a space."""
    return re.sub("%s", " ", arg)


def typed(commands: list[str]) -> str:
    """Replay the commands sent to the device into the text that ends up in the field."""
    keys = {"66": "\n", "61": "\t"}
    out = []
    for command in commands:
        argv = shlex.split(command)  # how the device shell splits the command line
        if argv[:2] == ["input", "text"]:
            assert len(argv) == 3, f"text leaked into extra shell words: {command}"
            out.append(device_input_text(argv[2]))
        else:
            assert argv[:2] == ["input", "keyevent"], command
            out.append(keys[argv[2]])
    return "".join(out)


def test_shell_special_characters_go_through_q(device, monkeypatch):
    quoted: list[str] = []

    def spy_q(value: str) -> str:
        quoted.append(value)
        return shlex.quote(value)

    monkeypatch.setattr(server, "q", spy_q)

    server.type_text(text=SHELL_SPECIAL)

    assert device.shell_commands, "nothing was sent"
    for command in device.shell_commands:
        arg = shlex.split(command)[2]
        assert command == f"input text {shlex.quote(arg)}"
        assert arg in quoted, f"{arg!r} reached the shell without q()"
    assert typed(device.shell_commands) == SHELL_SPECIAL


def test_spaces_are_sent_as_percent_s(device):
    server.type_text(text="hello world")

    assert device.shell_commands == ["input text hello%sworld"]


@pytest.mark.parametrize("text", ["100%sure", "%s", "50% off %s, 100%%s", "a%", "% s"])
def test_literal_percent_s_survives(device, text):
    server.type_text(text=text)

    assert typed(device.shell_commands) == text


def test_newline_and_tab_become_keyevents(device):
    server.type_text(text="user\tpass word\nok")

    assert device.shell_commands == [
        "input text user",
        "input keyevent 61",
        "input text pass%sword",
        "input keyevent 66",
        "input text ok",
    ]


def test_long_text_is_split_into_chunks(device):
    text = "x" * 450

    server.type_text(text=text)

    assert [len(shlex.split(c)[2]) for c in device.shell_commands] == [200, 200, 50]
    assert typed(device.shell_commands) == text


def test_empty_text_sends_nothing(device):
    assert server.type_text(text="") == "Nothing to type"
    assert device.shell_commands == []
