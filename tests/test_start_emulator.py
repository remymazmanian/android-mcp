"""start_emulator stays inside one MCP call and reports a boot that is still under way."""

import json

import pytest

from android_mcp import adb, server


class FakeEmulatorProcess:
    returncode = None

    def poll(self):
        return None


@pytest.fixture
def emulator(monkeypatch):
    """No real emulator: the AVD resolves, launching records the call, and time is simulated."""
    launched = []
    state = {"running": {}, "booted": False, "now": 0.0}
    monkeypatch.setattr(adb, "emulator_binary", lambda: "/sdk/emulator/emulator")
    monkeypatch.setattr(server, "_resolve_avd", lambda emulator, wanted: "Medium_Phone_API_37.0")
    monkeypatch.setattr(server, "_running_emulators", lambda: dict(state["running"]))
    monkeypatch.setattr(server, "_boot_completed", lambda serial: state["booted"])

    def popen(cmd, **kwargs):
        launched.append(cmd)
        state["running"]["emulator-5554"] = "Medium_Phone_API_37.0"
        return FakeEmulatorProcess()

    def sleep(seconds):
        state["now"] += seconds

    monkeypatch.setattr(server.subprocess, "Popen", popen)
    monkeypatch.setattr(server.time, "monotonic", lambda: state["now"])
    monkeypatch.setattr(server.time, "sleep", sleep)
    state["launched"] = launched
    return state


def test_default_wait_fits_inside_a_typical_client_timeout():
    import inspect

    default = inspect.signature(server.start_emulator).parameters["boot_timeout"].default
    assert default < 60


def test_a_slow_boot_returns_booting_instead_of_an_error(emulator):
    result = json.loads(server.start_emulator())

    assert result["status"] == "booting"
    assert result["serial"] == "emulator-5554"
    assert result["seconds"] <= 46
    assert "Call start_emulator again" in result["next_step"]
    assert len(emulator["launched"]) == 1


def test_calling_again_waits_on_the_same_emulator_without_launching_another(emulator):
    server.start_emulator()
    emulator["booted"] = True

    result = json.loads(server.start_emulator())

    assert result == {"status": "already_running", "avd": "Medium_Phone_API_37.0", "serial": "emulator-5554"}
    assert len(emulator["launched"]) == 1


def test_a_boot_that_finishes_in_time_reports_booted(emulator, monkeypatch):
    calls = {"n": 0}

    def boot_completed(serial):
        calls["n"] += 1
        return calls["n"] >= 3

    monkeypatch.setattr(server, "_boot_completed", boot_completed)

    result = json.loads(server.start_emulator())

    assert result["status"] == "booted"
    assert result["serial"] == "emulator-5554"
