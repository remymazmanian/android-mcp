"""Thin, safe wrapper around the adb binary.

Every adb invocation goes through `run()`: an argument list (never `shell=True`
on the Mac side), a timeout, and failures turned into `AdbError` with a message
that says what was attempted and what adb printed.

Commands executed *on the device* (`adb shell "<line>"`) are parsed by the
device's own shell, so every caller must quote untrusted values with `q()`.
"""

import os
import re
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

SDK_ROOT = Path.home() / "Library/Android/sdk"
DEFAULT_ADB = SDK_ROOT / "platform-tools/adb"
DEFAULT_EMULATOR = SDK_ROOT / "emulator/emulator"
DEFAULT_TIMEOUT = 30.0

q = shlex.quote


class AdbError(RuntimeError):
    """An adb call failed; the message is meant to be shown to the model as-is."""


def adb_binary() -> str:
    """Path to adb: $ANDROID_ADB if set, else the Android Studio SDK default."""
    path = Path(os.environ.get("ANDROID_ADB") or DEFAULT_ADB).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise AdbError(
            f"adb not found or not executable at {path}. Install Android SDK platform-tools "
            "or set ANDROID_ADB to the adb binary."
        )
    return str(path)


def emulator_binary() -> str:
    """Path to the emulator launcher: $ANDROID_EMULATOR if set, else the SDK default."""
    path = Path(os.environ.get("ANDROID_EMULATOR") or DEFAULT_EMULATOR).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise AdbError(f"Android emulator not found at {path}. Install it from the Android Studio SDK Manager.")
    return str(path)


def decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").strip()


def scrub(text: str, secrets: Sequence[str] | Mapping[str, str]) -> str:
    """Hide secrets in `text`, longest first.

    A sequence replaces each secret with "[redacted]"; a mapping gives each secret's replacement.
    Secrets of 4+ characters are replaced wherever they occur; shorter ones only as whole
    whitespace-separated tokens, so a one-letter secret can't shred the rest of the message.
    """
    replacements = dict(secrets) if isinstance(secrets, Mapping) else dict.fromkeys(secrets, "[redacted]")
    for secret in sorted((s for s in replacements if s), key=len, reverse=True):
        replacement = replacements[secret]
        if len(secret) >= 4:
            text = text.replace(secret, replacement)
        else:
            text = re.sub(rf"(?<!\S){re.escape(secret)}(?!\S)", lambda _: replacement, text)
    return text


def _describe(args: list[str], redact: Sequence[str] = ()) -> str:
    text = scrub("adb " + " ".join(args), redact)
    return text if len(text) <= 200 else text[:197] + "..."


def run(
    args: list[str],
    *,
    serial: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    check: bool = True,
    redact: Sequence[str] = (),
) -> subprocess.CompletedProcess[bytes]:
    """Run `adb [-s serial] <args...>` and return the completed process (stdout/stderr as bytes).

    Strings in `redact` (e.g. typed text) are replaced with "[redacted]" in any error message.
    """
    cmd = [adb_binary()]
    if serial:
        cmd += ["-s", serial]
    cmd += [str(a) for a in args]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired:
        raise AdbError(f"`{_describe(args, redact)}` timed out after {timeout:g}s") from None
    except OSError as e:
        raise AdbError(f"Could not run adb at {cmd[0]}: {e}") from None
    if check and proc.returncode != 0:
        detail = scrub(decode(proc.stderr) or decode(proc.stdout) or "no output", redact)
        raise AdbError(f"`{_describe(args, redact)}` failed (exit {proc.returncode}): {detail[:2000]}")
    return proc


def shell(
    command: str,
    *,
    serial: str,
    timeout: float = DEFAULT_TIMEOUT,
    check: bool = True,
    redact: Sequence[str] = (),
) -> str:
    """Run one command line in the device shell and return its stdout as text.

    `command` is interpreted by the device's shell: quote every untrusted value with `q()`.
    """
    return decode(run(["shell", command], serial=serial, timeout=timeout, check=check, redact=redact).stdout)


@dataclass
class Device:
    serial: str
    state: str
    details: dict[str, str] = field(default_factory=dict)

    @property
    def is_emulator(self) -> bool:
        return self.serial.startswith("emulator-")

    def as_dict(self) -> dict[str, str]:
        return {"serial": self.serial, "state": self.state, **self.details}


def list_devices() -> list[Device]:
    """Parse `adb devices -l`."""
    out = decode(run(["devices", "-l"], timeout=15).stdout)
    devices = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices", "*")):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        details = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        devices.append(Device(serial=parts[0], state=parts[1], details=details))
    return devices


def _emulator_port(device: Device) -> int:
    try:
        return int(device.serial.split("-", 1)[1])
    except (IndexError, ValueError):
        return 1 << 30


def select_serial() -> str:
    """Pick the one device every tool acts on.

    $ANDROID_SERIAL wins when set. Otherwise the first running emulator (lowest
    console port) is used. Physical devices are never picked implicitly.
    """
    devices = list_devices()
    summary = ", ".join(f"{d.serial} ({d.state})" for d in devices) or "none"
    wanted = os.environ.get("ANDROID_SERIAL", "").strip()
    if wanted:
        for d in devices:
            if d.serial == wanted:
                if d.state != "device":
                    raise AdbError(f"ANDROID_SERIAL={wanted} is connected but its state is '{d.state}', not 'device'.")
                return wanted
        raise AdbError(f"ANDROID_SERIAL={wanted} is not connected. Connected devices: {summary}.")

    online = [d for d in devices if d.state == "device"]
    emulators = sorted((d for d in online if d.is_emulator), key=_emulator_port)
    if emulators:
        return emulators[0].serial
    if online:
        raise AdbError(
            f"No emulator is running; only non-emulator devices are connected ({summary}). "
            "Set ANDROID_SERIAL to target one explicitly, or call start_emulator."
        )
    if devices:
        raise AdbError(f"No device is ready yet ({summary}). If an emulator is booting, wait and retry.")
    raise AdbError("No Android device or emulator is connected. Call start_emulator to boot one.")


def getprops(serial: str) -> dict[str, str]:
    """All system properties of the device, from one `getprop` call."""
    props = {}
    for line in shell("getprop", serial=serial).splitlines():
        m = re.match(r"^\[(.+?)\]: \[(.*)\]$", line.strip())
        if m:
            props[m.group(1)] = m.group(2)
    return props


def screen_size(serial: str) -> tuple[int, int]:
    """Current display size in pixels, in the current orientation (the frame input/tap uses)."""
    out = shell("dumpsys window displays", serial=serial, check=False)
    m = re.search(r"\bcur=(\d+)x(\d+)", out)
    if m:
        return int(m.group(1)), int(m.group(2))
    out = shell("wm size", serial=serial)
    m = re.search(r"Override size: (\d+)x(\d+)", out) or re.search(r"Physical size: (\d+)x(\d+)", out)
    if not m:
        raise AdbError(f"Could not determine screen size from `wm size`: {out!r}")
    return int(m.group(1)), int(m.group(2))


def emulator_avd_name(serial: str) -> str | None:
    """AVD name of a running emulator via its console (works while it is still booting)."""
    proc = run(["emu", "avd", "name"], serial=serial, timeout=5, check=False)
    lines = [ln.strip() for ln in decode(proc.stdout).splitlines() if ln.strip() and ln.strip() != "OK"]
    return lines[0] if proc.returncode == 0 and lines else None
