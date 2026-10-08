"""Shared fixtures: every test runs against a fake device and temporary folders.

No test needs an emulator, and none writes outside pytest's tmp_path: the action log,
the files folder and even $HOME are redirected there.
"""

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from android_mcp import adb, sandbox, server


class FakeDevice:
    """Stands in for adb.run / adb.shell: records every call and answers from canned replies."""

    def __init__(self) -> None:
        self.shell_commands: list[str] = []
        self.run_calls: list[list[str]] = []
        self.shell_replies: dict[str, str] = {}  # command prefix -> stdout
        self.run_replies: dict[str, bytes] = {}  # first adb argument ("install", "shell"...) -> stdout

    def shell(self, command: str, *, serial: str, timeout: float = 30, check: bool = True, redact=()) -> str:
        self.shell_commands.append(command)
        return next((reply for prefix, reply in self.shell_replies.items() if command.startswith(prefix)), "")

    def run(self, args, *, serial=None, timeout=30, check=True, redact=()) -> subprocess.CompletedProcess[bytes]:
        args = [str(a) for a in args]
        self.run_calls.append(args)
        return subprocess.CompletedProcess(["adb", *args], 0, self.run_replies.get(args[0], b""), b"")

    def calls(self, verb: str) -> list[list[str]]:
        """adb.run calls whose first argument is `verb`, e.g. calls("push")."""
        return [c for c in self.run_calls if c[0] == verb]


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect logs, the files folder and $HOME into tmp_path; make any real adb call fail loudly."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(server, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(sandbox, "DEFAULT_FILES_DIR", tmp_path / "default-files")
    monkeypatch.setenv(sandbox.FILES_DIR_ENV, str(tmp_path / "files"))
    monkeypatch.delenv("ANDROID_MCP_ALLOW_SHELL", raising=False)
    monkeypatch.delenv("ANDROID_SERIAL", raising=False)

    def no_real_adb() -> str:
        raise AssertionError("a test tried to run the real adb binary")

    monkeypatch.setattr(adb, "adb_binary", no_real_adb)
    return tmp_path


@pytest.fixture
def device(monkeypatch: pytest.MonkeyPatch) -> FakeDevice:
    fake = FakeDevice()
    monkeypatch.setattr(adb, "select_serial", lambda: "emulator-5554")
    monkeypatch.setattr(adb, "shell", fake.shell)
    monkeypatch.setattr(adb, "run", fake.run)
    monkeypatch.setattr(adb, "screen_size", lambda serial: (1080, 2400))
    return fake


@pytest.fixture
def files_root(isolated: Path) -> Path:
    """The (resolved, freshly created) allowed files folder for this test."""
    return sandbox.files_root()


@pytest.fixture
def outside(isolated: Path) -> Path:
    """A folder next to the files folder, holding things that must never be reachable."""
    folder = isolated / "outside"
    folder.mkdir()
    (folder / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----")
    (folder / "evil.apk").write_bytes(b"not really an apk")
    return folder


@pytest.fixture
def action_log() -> Callable[[], str]:
    """Read the action log the server wrote during this test."""

    def read() -> str:
        path = server.LOG_DIR / "actions.log"
        return path.read_text() if path.exists() else ""

    return read
