"""MCP server that lets AI assistants control an Android emulator through adb (stdio transport)."""

import functools
import io
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, TypeVar
from xml.etree.ElementTree import ParseError

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from PIL import Image as PILImage
from pydantic import Field

from . import adb, ui
from .adb import AdbError, q

PROJECT_DIR = Path(__file__).resolve().parents[2]
if not (PROJECT_DIR / "pyproject.toml").is_file():
    PROJECT_DIR = Path.home() / "agent-tools/android-mcp"
LOG_DIR = PROJECT_DIR / "logs"

INSTRUCTIONS = """\
Controls one Android device (by default the first running emulator) over adb.

Coordinates: every x/y argument (tap, long_press, swipe) is in FULL-RESOLUTION device
pixels, origin at the top-left, in the current orientation. ui_dump `center` values are
already in that frame and can be passed straight to tap. screenshot returns a downscaled
image plus its `scale`: convert image pixels with full = image_px / scale.

Typical loop: ui_dump (cheap, exact) or screenshot (visual) -> tap_element / tap /
type_text -> wait_for_element or screenshot to confirm the result.
"""

mcp = MCPServer(name="android", instructions=INSTRUCTIONS, version="0.1.0")

# --------------------------------------------------------------------------- logging

_log_lock = threading.Lock()


def _write_log(line: str) -> None:
    """Append one line to LOG_DIR/actions.log (LOG_DIR is read at call time, so tests can override it)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with _log_lock, open(LOG_DIR / "actions.log", "a", encoding="utf-8") as log:
        log.write(line + "\n")


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _summarize(result: Any) -> str:
    if isinstance(result, list | tuple):
        return " + ".join(_summarize(r) for r in result)
    if isinstance(result, Image):
        return f"image {getattr(result, '_mime_type', '?')} {len(result.data or b'') // 1024}KB"
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)


def _log_call(tool_name: str, args: dict[str, Any], status: str, summary: str, started: float) -> None:
    shown = {k: _short(v, 200) if isinstance(v, str) else v for k, v in args.items()}
    _write_log(
        " | ".join(
            [
                datetime.now().astimezone().isoformat(timespec="milliseconds"),
                tool_name,
                json.dumps(shown, ensure_ascii=False, default=str),
                status,
                f"{time.monotonic() - started:.2f}s",
                _short(summary, 300),
            ]
        )
    )


F = TypeVar("F", bound=Callable[..., Any])


def tool(*, read_only: bool = False, destructive: bool = False) -> Callable[[F], F]:
    """Register a sync function as an MCP tool, with action logging and readable errors.

    The SDK runs sync tools in a worker thread, so slow adb calls never block the server.
    """
    annotations = ToolAnnotations(read_only_hint=read_only, destructive_hint=None if read_only else destructive)

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(**kwargs: Any) -> Any:
            started = time.monotonic()
            try:
                result = fn(**kwargs)
            except (AdbError, ToolError) as e:
                _log_call(fn.__name__, kwargs, "error", str(e), started)
                raise ToolError(str(e)) from None
            except Exception as e:
                message = f"{type(e).__name__}: {e}"
                _log_call(fn.__name__, kwargs, "crash", message, started)
                raise ToolError(f"{fn.__name__} failed unexpectedly: {message}") from e
            _log_call(fn.__name__, kwargs, "ok", _summarize(result), started)
            return result

        mcp.tool(annotations=annotations, structured_output=False)(wrapper)
        return fn

    return decorator


# --------------------------------------------------------------------------- helpers


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def _serial() -> str:
    return adb.select_serial()


def _sh(serial: str, command: str, timeout: float = adb.DEFAULT_TIMEOUT, check: bool = True) -> str:
    return adb.shell(command, serial=serial, timeout=timeout, check=check)


def _check_point(serial: str, x: int, y: int, what: str = "Point") -> None:
    width, height = adb.screen_size(serial)
    if not (0 <= x < width and 0 <= y < height):
        raise ToolError(
            f"{what} ({x}, {y}) is outside the {width}x{height} screen. Coordinates are full-resolution "
            "pixels; if you measured them on a downscaled screenshot, divide by its scale first."
        )


_dump_lock = threading.Lock()
_REMOTE_DUMP = "/sdcard/ui.xml"


def _dump(serial: str) -> tuple[list[ui.Element], dict[str, Any]]:
    """Fresh uiautomator dump of the current screen, parsed into elements."""
    with _dump_lock:
        last = ""
        for _ in range(3):
            out = _sh(serial, f"uiautomator dump {_REMOTE_DUMP}", timeout=30, check=False)
            if "dumped to" in out:
                break
            last = out
            time.sleep(0.5)
        else:
            raise ToolError(f"uiautomator dump failed 3 times (the screen may be animating): {last or 'no output'}")
        xml_text = adb.decode(adb.run(["exec-out", "cat", _REMOTE_DUMP], serial=serial).stdout)
    try:
        return ui.parse_hierarchy(xml_text)
    except ParseError as e:
        raise ToolError(f"Could not parse the uiautomator dump: {e}") from None


def _match_args(text: str | None, desc: str | None, resource_id: str | None) -> str:
    parts = [f"{k}={v!r}" for k, v in (("text", text), ("desc", desc), ("resource_id", resource_id)) if v]
    return ", ".join(parts)


def _not_found(elements: list[ui.Element], text: str | None, desc: str | None, resource_id: str | None) -> str:
    near = ui.closest(elements, text=text, desc=desc, resource_id=resource_id)
    hint = "; ".join(e.describe() for e in near) if near else "none (call ui_dump to see what is on screen)"
    return f"Closest matches: {hint}"


def _boot_completed(serial: str) -> bool:
    try:
        return _sh(serial, "getprop sys.boot_completed", timeout=10, check=False) == "1"
    except AdbError:
        return False


def _running_emulators() -> dict[str, str]:
    """serial -> AVD name for every emulator adb knows about (including ones still booting)."""
    running = {}
    for device in adb.list_devices():
        if device.is_emulator:
            name = adb.emulator_avd_name(device.serial)
            if name:
                running[device.serial] = name
    return running


def _foreground(serial: str) -> dict[str, str | None]:
    out = _sh(serial, "dumpsys activity activities", check=False)
    m = re.search(
        r"(?:topResumedActivity|mResumedActivity|ResumedActivity)[=:]\s*ActivityRecord\{\S+ \S+ ([\w.]+)/([\w.$]+)", out
    )
    package = activity = None
    if m:
        package, activity = m.groups()
        if activity.startswith("."):
            activity = package + activity
    window = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([^}]+)\}", _sh(serial, "dumpsys window", check=False))
    return {"package": package, "activity": activity, "focused_window": window.group(1) if window else None}


_PACKAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+")


def _require_package(serial: str, package: str) -> None:
    if not _PACKAGE_RE.fullmatch(package):
        raise ToolError(f"'{package}' is not a valid package name (expected something like com.android.settings).")
    if "package:" not in _sh(serial, f"pm path {q(package)}", check=False):
        raise ToolError(f"Package {package} is not installed. Use list_packages to find the right name.")


def _remote_exists(serial: str, path: str) -> bool:
    return _sh(serial, f"test -e {q(path)} && echo yes", check=False) == "yes"


def _unique_local_path(path: Path) -> Path:
    """`path`, or `name (1).ext`, `name (2).ext`... so existing Mac files are never overwritten."""
    if not path.exists():
        return path
    for n in range(1, 10_000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise ToolError(f"Could not find a free file name next to {path}")


def _unique_remote_path(serial: str, path: str) -> str:
    if not _remote_exists(serial, path):
        return path
    p = PurePosixPath(path)
    for n in range(1, 10_000):
        candidate = str(p.with_name(f"{p.stem} ({n}){p.suffix}"))
        if not _remote_exists(serial, candidate):
            return candidate
    raise ToolError(f"Could not find a free file name next to {path} on the device")


# --------------------------------------------------------------------------- device / emulator


@tool(read_only=True)
def list_devices() -> str:
    """List connected Android devices and emulators (`adb devices -l`) and show which one the tools act on.

    The selected device is $ANDROID_SERIAL when set, otherwise the first running emulator-* device.
    Physical devices are only used when named in ANDROID_SERIAL.
    """
    result: dict[str, Any] = {"devices": [d.as_dict() for d in adb.list_devices()]}
    try:
        result["selected"] = adb.select_serial()
    except AdbError as e:
        result["selected"] = None
        result["note"] = str(e)
    return _json(result)


_emulator_processes: list[subprocess.Popen[bytes]] = []


def _resolve_avd(emulator: str, wanted: str) -> str:
    try:
        proc = subprocess.run([emulator, "-list-avds"], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ToolError(f"Could not list AVDs with {emulator}: {e}") from None
    avds = [
        line.strip()
        for line in adb.decode(proc.stdout).splitlines()
        if line.strip() and " " not in line.strip() and not line.startswith(("INFO", "WARNING", "ERROR"))
    ]
    if wanted in avds:
        return wanted
    prefixed = [a for a in avds if a.casefold().startswith(wanted.casefold())]
    if len(prefixed) == 1:
        return prefixed[0]
    if not avds:
        raise ToolError("No AVDs exist. Create one in Android Studio's Device Manager.")
    reason = "matches several AVDs" if prefixed else "was not found"
    raise ToolError(f"AVD '{wanted}' {reason}. Available AVDs: {', '.join(avds)}")


def _tail(path: Path, lines: int = 15) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log)"


@tool()
def start_emulator(
    avd: Annotated[str, Field(description="AVD name from `emulator -list-avds`; a unique prefix also works.")] = "Medium_Phone",
    boot_timeout: Annotated[int, Field(description="Seconds to wait for Android to finish booting.", ge=10, le=900)] = 180,
) -> str:
    """Launch an Android Virtual Device in the background and wait until sys.boot_completed == 1.

    The default "Medium_Phone" matches the AVD "Medium_Phone_API_37.0" by prefix. If that AVD is
    already running this returns at once. The emulator window keeps running after this server
    exits; use stop_emulator to shut it down. Emulator output goes to logs/emulator.log.
    """
    emulator = adb.emulator_binary()
    name = _resolve_avd(emulator, avd)
    started = time.monotonic()
    running = _running_emulators()
    proc: subprocess.Popen[bytes] | None = None
    target = next((s for s, n in running.items() if n == name), None)
    if target and _boot_completed(target):
        return _json({"status": "already_running", "avd": name, "serial": target})
    if target is None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "emulator.log", "ab") as log:
            log.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} starting {name}\n".encode())
            log.flush()
            proc = subprocess.Popen(
                [emulator, "-avd", name],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # detached: survives this server exiting
            )
        _emulator_processes.append(proc)

    deadline = started + boot_timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise ToolError(
                f"The emulator exited with code {proc.returncode} before booting. "
                f"Last lines of logs/emulator.log:\n{_tail(LOG_DIR / 'emulator.log')}"
            )
        if target is None:
            target = next((s for s, n in _running_emulators().items() if n == name and s not in running), None)
        if target and _boot_completed(target):
            return _json(
                {"status": "booted", "avd": name, "serial": target, "seconds": round(time.monotonic() - started, 1)}
            )
        time.sleep(2)
    raise ToolError(
        f"{name} did not finish booting within {boot_timeout}s (serial: {target or 'not visible to adb yet'}). "
        "It may still be starting; call start_emulator again to keep waiting."
    )


@tool(destructive=True)
def stop_emulator() -> str:
    """Shut down the selected emulator (`adb emu kill`). Refuses to act on physical devices."""
    serial = _serial()
    if not serial.startswith("emulator-"):
        raise ToolError(f"{serial} is not an emulator; stop_emulator only shuts down emulators.")
    avd = adb.emulator_avd_name(serial)
    adb.run(["emu", "kill"], serial=serial, timeout=15)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and any(d.serial == serial for d in adb.list_devices()):
        time.sleep(1)
    gone = not any(d.serial == serial for d in adb.list_devices())
    return _json({"stopped": serial, "avd": avd, "disconnected": gone})


@tool(read_only=True)
def device_info() -> str:
    """Model, Android version, screen size (`wm size`), density (`wm density`) and AVD name of the selected device.

    `current_resolution` is the frame all tap/swipe coordinates use (it follows rotation).
    """
    serial = _serial()
    props = adb.getprops(serial)
    width, height = adb.screen_size(serial)
    sizes = dict(re.findall(r"(Physical|Override) size: (\d+x\d+)", _sh(serial, "wm size")))
    densities = dict(re.findall(r"(Physical|Override) density: (\d+)", _sh(serial, "wm density")))
    info: dict[str, Any] = {
        "serial": serial,
        "model": props.get("ro.product.model"),
        "manufacturer": props.get("ro.product.manufacturer"),
        "android_version": props.get("ro.build.version.release"),
        "sdk": props.get("ro.build.version.sdk"),
        "build": props.get("ro.build.display.id"),
        "abi": props.get("ro.product.cpu.abi"),
        "screen_size": sizes.get("Override", sizes.get("Physical")),
        "physical_screen_size": sizes.get("Physical"),
        "current_resolution": f"{width}x{height}",
        "density_dpi": int(densities.get("Override", densities.get("Physical", "0"))),
        "boot_completed": props.get("sys.boot_completed") == "1",
    }
    if serial.startswith("emulator-"):
        info["avd"] = adb.emulator_avd_name(serial)
    return _json(info)


# --------------------------------------------------------------------------- seeing the screen


@tool(read_only=True)
def screenshot(
    scale: Annotated[float, Field(description="Downscale factor for the returned image (0.1-1.0).", ge=0.1, le=1.0)] = 0.5,
    image_format: Annotated[Literal["jpeg", "png"], Field(description="jpeg is smaller; png is lossless.")] = "jpeg",
) -> list[Any]:
    """Capture the screen (`screencap -p`) and return it as an image, downscaled by `scale`.

    Also returns the full-resolution size and the scale. Tap/swipe tools take FULL-RESOLUTION
    coordinates, so convert a point measured on this image with full = image_px / scale
    (at the default 0.5: double it). For exact element positions prefer ui_dump.
    """
    serial = _serial()
    png = adb.run(["exec-out", "screencap", "-p"], serial=serial, timeout=30).stdout
    if not png.startswith(b"\x89PNG"):
        raise ToolError(f"screencap did not return a PNG: {adb.decode(png[:200]) or 'empty output'}")
    image = PILImage.open(io.BytesIO(png))
    image.load()
    full_w, full_h = image.size
    if scale < 1.0:
        size = (max(1, round(full_w * scale)), max(1, round(full_h * scale)))
        image = image.resize(size, PILImage.Resampling.LANCZOS)
    buffer = io.BytesIO()
    if image_format == "jpeg":
        image.convert("RGB").save(buffer, "JPEG", quality=80, optimize=True)
    else:
        image.save(buffer, "PNG", optimize=True)
    meta = {
        "full_resolution": [full_w, full_h],
        "image_size": list(image.size),
        "scale": scale,
        "coordinates": f"tap/swipe use full-resolution pixels: full_x = image_x / {scale}, full_y = image_y / {scale}",
    }
    return [Image(data=buffer.getvalue(), format=image_format), json.dumps(meta)]


@tool(read_only=True)
def ui_dump() -> str:
    """List the on-screen UI elements from a fresh `uiautomator dump`, one JSON object per line.

    Each element has: index, text, desc (content-description), resource_id, class, clickable,
    bounds [x1, y1, x2, y2] and center [x, y], all in FULL-RESOLUTION pixels, so `center` can
    be passed straight to tap. Clickable containers without their own text get a `label` built
    from their children's text. Flags such as scrollable, checked/unchecked, selected, focused
    and password appear only when true. Plain layout containers are omitted.
    Indexes are only valid for this dump; they change when the screen changes.
    """
    serial = _serial()
    elements, meta = _dump(serial)
    width, height = adb.screen_size(serial)
    header = {"packages": meta["packages"], "rotation": meta["rotation"], "screen": [width, height], "count": len(elements)}
    lines = ",\n".join(json.dumps(e.to_dict(), ensure_ascii=False) for e in elements)
    return json.dumps(header, ensure_ascii=False)[:-1] + f', "elements": [\n{lines}\n]}}'


# --------------------------------------------------------------------------- acting

Coord = Annotated[int, Field(description="Full-resolution device pixels (origin top-left).", ge=0)]


@tool()
def tap(x: Coord, y: Coord) -> str:
    """Tap the screen at (x, y), in FULL-RESOLUTION device pixels, origin top-left, current orientation.

    ui_dump `center` values can be used as-is. A point measured on a screenshot must be divided by
    that screenshot's scale first (at scale 0.5, image point (270, 400) -> tap(540, 800)).
    """
    serial = _serial()
    _check_point(serial, x, y)
    _sh(serial, f"input tap {x} {y}")
    return f"Tapped ({x}, {y})"


@tool()
def tap_element(
    text: Annotated[str | None, Field(description="Visible text to match (exact first, then substring; case-insensitive).")] = None,
    desc: Annotated[str | None, Field(description="content-description (accessibility label) to match.")] = None,
    resource_id: Annotated[str | None, Field(description="Resource id, full ('com.app:id/title') or short ('title').")] = None,
    index: Annotated[int | None, Field(description="Element index from the most recent ui_dump; use alone.", ge=0)] = None,
) -> str:
    """Find an element in a fresh ui_dump and tap its center.

    Give text, desc and/or resource_id (all given criteria must match), or `index` alone.
    Exact matches win over substring matches; tappable elements win over plain labels.
    If nothing matches, the error lists the closest candidates. `index` is only safe when the
    screen has not changed since the ui_dump that produced it.
    """
    if index is not None and (text or desc or resource_id):
        raise ToolError("Use either index or text/desc/resource_id, not both.")
    if index is None and not (text or desc or resource_id):
        raise ToolError("Give text, desc, resource_id, or index.")
    serial = _serial()
    elements, _ = _dump(serial)
    others = 0
    if index is not None:
        if index >= len(elements):
            raise ToolError(f"Index {index} is out of range: the current screen has {len(elements)} elements.")
        element = elements[index]
    else:
        matches = ui.find(elements, text=text, desc=desc, resource_id=resource_id)
        if not matches:
            raise ToolError(
                f"No element matches {_match_args(text, desc, resource_id)}. "
                + _not_found(elements, text, desc, resource_id)
            )
        element, others = matches[0], len(matches) - 1
    x, y = element.center
    _sh(serial, f"input tap {x} {y}")
    note = f" ({others} other match{'es' if others != 1 else ''} ignored)" if others else ""
    return f"Tapped {element.describe()}{note}"


@tool()
def long_press(
    x: Coord,
    y: Coord,
    ms: Annotated[int, Field(description="Hold duration in milliseconds.", ge=100, le=10_000)] = 800,
) -> str:
    """Press and hold at (x, y) in FULL-RESOLUTION device pixels for `ms` milliseconds."""
    serial = _serial()
    _check_point(serial, x, y)
    _sh(serial, f"input swipe {x} {y} {x} {y} {ms}")
    return f"Long-pressed ({x}, {y}) for {ms} ms"


@tool()
def swipe(
    x1: Coord,
    y1: Coord,
    x2: Coord,
    y2: Coord,
    ms: Annotated[int, Field(description="Gesture duration in milliseconds (shorter = faster fling).", ge=10, le=10_000)] = 300,
) -> str:
    """Swipe from (x1, y1) to (x2, y2), in FULL-RESOLUTION device pixels, over `ms` milliseconds.

    Remember that the finger moves opposite to the content: swiping up scrolls a list down.
    """
    serial = _serial()
    _check_point(serial, x1, y1, "Start point")
    _check_point(serial, x2, y2, "End point")
    _sh(serial, f"input swipe {x1} {y1} {x2} {y2} {ms}")
    return f"Swiped ({x1}, {y1}) -> ({x2}, {y2}) in {ms} ms"


@tool()
def scroll(
    direction: Annotated[
        Literal["down", "up", "left", "right"], Field(description="Which way to move through the content.")
    ] = "down",
) -> str:
    """Scroll the content from the middle of the screen, by about half a screen.

    "down" reveals content further down (like scrolling a web page down; the finger swipes up),
    "up" goes back toward the top, "right" reveals content to the right, "left" to the left.
    """
    serial = _serial()
    width, height = adb.screen_size(serial)
    cx, cy, dx, dy = width // 2, height // 2, int(width * 0.3), int(height * 0.25)
    x1, y1, x2, y2 = {
        "down": (cx, cy + dy, cx, cy - dy),
        "up": (cx, cy - dy, cx, cy + dy),
        "right": (cx + dx, cy, cx - dx, cy),
        "left": (cx - dx, cy, cx + dx, cy),
    }[direction]
    _sh(serial, f"input swipe {x1} {y1} {x2} {y2} 300")
    return f"Scrolled {direction} (swipe ({x1}, {y1}) -> ({x2}, {y2}))"


_ASCII_PRINTABLE = frozenset(chr(c) for c in range(0x20, 0x7F))


def _input_text_args(segment: str, chunk: int = 200) -> list[str]:
    """Split ASCII text into `input text` arguments.

    `input text` turns "%s" into a space and has no escape for a literal "%s", so a literal
    "%s" is split across two calls ("%" | "s..."); then spaces are encoded as "%s".
    Shell-special characters are handled by quoting each argument for the device shell.
    """
    parts = segment.split("%s")
    pieces = [("s" if i else "") + p + ("%" if i < len(parts) - 1 else "") for i, p in enumerate(parts)]
    args = []
    for piece in pieces:
        for start in range(0, len(piece), chunk):
            args.append(piece[start : start + chunk].replace(" ", "%s"))
    return [a for a in args if a]


def _paste_via_clipboard(serial: str, text: str) -> str:
    probe = adb.run(["shell", "cmd clipboard"], serial=serial, timeout=10, check=False)
    probe_out = adb.decode(probe.stdout + probe.stderr)
    if "No shell command implementation" in probe_out or "Can't find service" in probe_out:
        raise ToolError(
            "Nothing was typed: the text contains non-ASCII characters (emoji, accents, CJK, curly quotes...), "
            "which `adb shell input text` cannot send, and this Android build has no `cmd clipboard` shell "
            "command for the paste fallback. Rewrite the text in plain ASCII, or type the ASCII parts and "
            "ask the user to enter the rest."
        )
    result = adb.run(["shell", f"cmd clipboard set-text {q(text)}"], serial=serial, timeout=10, check=False)
    out = adb.decode(result.stdout + result.stderr)
    if result.returncode != 0 or re.search(r"(?i)unknown|error|usage", out):
        raise ToolError(f"Nothing was typed: setting the device clipboard failed: {out or result.returncode}")
    _sh(serial, "input keyevent 279")  # KEYCODE_PASTE
    return f"Pasted {len(text)} characters via the device clipboard"


@tool()
def type_text(text: Annotated[str, Field(description="Text to type into the focused field.")]) -> str:
    """Type text into the currently focused input field (tap the field first).

    ASCII text is sent with `adb shell input text`; spaces and shell-special characters
    (' " $ & ; | < > ( ) * ? ! ` \\ %) are escaped for you, and newline/tab press Enter/Tab.
    Non-ASCII text (emoji, accented letters, CJK, curly quotes) cannot go through `input text`;
    for it the tool falls back to setting the device clipboard (`cmd clipboard`) and pasting.
    Limitation: stock emulator images (including Android 17) do not ship `cmd clipboard`, so on
    them non-ASCII text returns an error and nothing is typed.
    """
    if not text:
        return "Nothing to type"
    serial = _serial()
    if not all(c in _ASCII_PRINTABLE or c in "\n\t" for c in text):
        return _paste_via_clipboard(serial, text)
    for token in re.split(r"(\n|\t)", text):
        if token == "\n":
            _sh(serial, "input keyevent 66")
        elif token == "\t":
            _sh(serial, "input keyevent 61")
        else:
            for arg in _input_text_args(token):
                _sh(serial, f"input text {q(arg)}")
    return f"Typed {len(text)} characters"


KEYCODES = {
    "back": 4,
    "home": 3,
    "recents": 187,
    "app_switch": 187,
    "enter": 66,
    "delete": 67,
    "backspace": 67,
    "forward_delete": 112,
    "tab": 61,
    "space": 62,
    "escape": 111,
    "menu": 82,
    "search": 84,
    "volume_up": 24,
    "volume_down": 25,
    "volume_mute": 164,
    "power": 26,
    "wakeup": 224,
    "sleep": 223,
    "dpad_up": 19,
    "dpad_down": 20,
    "dpad_left": 21,
    "dpad_right": 22,
    "dpad_center": 23,
    "page_up": 92,
    "page_down": 93,
    "move_home": 122,
    "move_end": 123,
    "copy": 278,
    "paste": 279,
    "cut": 277,
    "notifications": 83,
}


@tool()
def press_key(
    key: Annotated[str, Field(description="Key name such as back, home, recents, enter, delete, volume_up.")],
) -> str:
    """Press a key via `input keyevent`.

    Names: back, home, recents, enter, delete (backspace), forward_delete, tab, space, escape,
    menu, search, volume_up, volume_down, volume_mute, power (toggles the screen), wakeup, sleep,
    dpad_up/down/left/right/center, page_up, page_down, move_home, move_end, copy, paste, cut,
    notifications. Any Android keycode name ("KEYCODE_CAMERA") or number ("27") also works.
    """
    name = key.strip().lower().replace("-", "_").replace(" ", "_")
    if name in KEYCODES:
        code = str(KEYCODES[name])
    elif re.fullmatch(r"\d{1,3}", name):
        code = name
    elif re.fullmatch(r"keycode_[a-z0-9_]+", name):
        code = name.upper()
    else:
        raise ToolError(f"Unknown key '{key}'. Known names: {', '.join(KEYCODES)}; or a KEYCODE_* name or number.")
    serial = _serial()
    out = _sh(serial, f"input keyevent {code}", check=False)
    if out:
        raise ToolError(f"input keyevent {code} failed: {out}")
    return f"Pressed {key} (keycode {code})"


@tool(read_only=True)
def wait(seconds: Annotated[float, Field(description="How long to wait.", gt=0, le=60)]) -> str:
    """Do nothing for `seconds` (up to 60), e.g. while an app loads or an animation finishes."""
    time.sleep(seconds)
    return f"Waited {seconds:g}s"


@tool(read_only=True)
def wait_for_element(
    text: Annotated[str | None, Field(description="Visible text to wait for (exact, then substring).")] = None,
    desc: Annotated[str | None, Field(description="content-description to wait for.")] = None,
    resource_id: Annotated[str | None, Field(description="Resource id, full or short form.")] = None,
    timeout: Annotated[float, Field(description="Seconds to keep polling.", gt=0, le=120)] = 15,
) -> str:
    """Poll ui_dump until an element matching all given criteria appears, then return it.

    Returns the element (with its full-resolution center) or errors after `timeout` seconds
    with the closest candidates on the last screen.
    """
    if not (text or desc or resource_id):
        raise ToolError("Give text, desc and/or resource_id to wait for.")
    serial = _serial()
    started = time.monotonic()
    elements: list[ui.Element] = []
    while True:
        try:
            elements, _ = _dump(serial)
            matches = ui.find(elements, text=text, desc=desc, resource_id=resource_id)
            if matches:
                return _json(
                    {
                        "found": matches[0].to_dict(),
                        "matches": len(matches),
                        "waited_seconds": round(time.monotonic() - started, 1),
                    }
                )
        except (AdbError, ToolError):
            pass  # transient dump failures (animations, app switching): keep polling
        if time.monotonic() - started >= timeout:
            break
        time.sleep(0.5)
    raise ToolError(
        f"No element matching {_match_args(text, desc, resource_id)} appeared within {timeout:g}s. "
        + _not_found(elements, text, desc, resource_id)
    )


# --------------------------------------------------------------------------- apps


@tool(read_only=True)
def list_packages(
    filter: Annotated[str | None, Field(description="Case-insensitive substring to filter package names.")] = None,
    third_party_only: Annotated[bool, Field(description="Only apps the user installed (pm list packages -3).")] = False,
) -> str:
    """List installed package names (`pm list packages`), optionally filtered by substring."""
    serial = _serial()
    out = _sh(serial, "pm list packages" + (" -3" if third_party_only else ""))
    packages = sorted(line.removeprefix("package:").strip() for line in out.splitlines() if line.startswith("package:"))
    if filter:
        packages = [p for p in packages if filter.casefold() in p.casefold()]
    return _json({"count": len(packages), "packages": packages})


@tool()
def launch_app(package: Annotated[str, Field(description="Package name, e.g. com.android.settings.")]) -> str:
    """Launch an app by package name through its launcher activity (`monkey -p <pkg> -c LAUNCHER 1`).

    If the app is already running, its existing task is brought to the front.
    """
    serial = _serial()
    _require_package(serial, package)
    out = _sh(serial, f"monkey -p {q(package)} -c android.intent.category.LAUNCHER 1", check=False)
    if "No activities found" in out or "aborted" in out:
        raise ToolError(f"{package} has no launcher activity to start: {_short(out, 300)}")
    time.sleep(1.0)
    return _json({"launched": package, "foreground": _foreground(serial)})


@tool(destructive=True)
def force_stop(package: Annotated[str, Field(description="Package name to stop.")]) -> str:
    """Force-stop an app (`am force-stop`). Its process is killed; unsaved in-app state is lost."""
    serial = _serial()
    _require_package(serial, package)
    _sh(serial, f"am force-stop {q(package)}")
    return f"Force-stopped {package}"


@tool(read_only=True)
def current_app() -> str:
    """The foreground app: package, activity, and the window that has input focus (dialogs, IME, shade)."""
    return _json(_foreground(_serial()))


@tool()
def install_apk(path: Annotated[str, Field(description="Path to a .apk file on this Mac (~ is expanded).")]) -> str:
    """Install (or update in place, keeping data) an APK from this Mac with `adb install -r`."""
    apk = Path(path).expanduser()
    if not apk.is_file():
        raise ToolError(f"APK not found: {apk}")
    if apk.suffix.lower() != ".apk":
        raise ToolError(f"{apk.name} is not an .apk file (split APK bundles are not supported).")
    serial = _serial()
    proc = adb.run(["install", "-r", str(apk)], serial=serial, timeout=600, check=False)
    out = adb.decode(proc.stdout + b"\n" + proc.stderr)
    if proc.returncode != 0 or "Success" not in out:
        raise ToolError(f"Installing {apk.name} failed: {_short(out, 1000)}")
    return f"Installed {apk.name}"


@tool()
def open_url(url: Annotated[str, Field(description="URL or URI with a scheme: https://..., tel:..., geo:..., market://...")]) -> str:
    """Open a URL/URI with `am start -a android.intent.action.VIEW -d <url>` (browser, deep link, etc.)."""
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", url):
        raise ToolError("The URL needs a scheme, e.g. https://example.com")
    serial = _serial()
    out = _sh(serial, f"am start -W -a android.intent.action.VIEW -d {q(url)}", timeout=45, check=False)
    if re.search(r"(?m)^Error", out) or "Exception" in out:
        raise ToolError(f"Could not open {url}: {_short(out, 500)}")
    status = dict(re.findall(r"(?m)^(Status|Activity|LaunchState): (.+)$", out))
    return _json({"opened": url, **{k.lower(): v for k, v in status.items()}})


# --------------------------------------------------------------------------- files


def _in_mediastore(serial: str, device_path: str) -> bool:
    where = "_data='" + device_path.replace("'", "''") + "'"
    out = _sh(serial, f"content query --uri content://media/external/file --projection _id --where {q(where)}", check=False)
    return "Row:" in out


def _media_scan(serial: str, remote_path: str, is_dir: bool) -> dict[str, Any]:
    """Make a pushed file visible to gallery/file pickers right away."""
    real = _sh(serial, f"readlink -f {q(remote_path)}", check=False) or remote_path
    scan_cmd = f"content call --uri content://media/ --method scan_file --arg {q(real)}"
    if is_dir:
        out = _sh(serial, scan_cmd, timeout=120, check=False)
        return {"method": "content call scan_file (directory)", "ok": out.startswith("Result")}
    uri = "file://" + urllib.parse.quote(real)
    _sh(serial, f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d {q(uri)}", check=False)
    methods = ["am broadcast MEDIA_SCANNER_SCAN_FILE"]
    for fallback in (None, scan_cmd):
        if fallback:
            _sh(serial, fallback, timeout=60, check=False)
            methods.append("content call scan_file")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if _in_mediastore(serial, real):
                return {"methods": methods, "indexed_in_mediastore": True}
            time.sleep(0.5)
    return {"methods": methods, "indexed_in_mediastore": False}


@tool()
def push_file(
    local_path: Annotated[str, Field(description="File or folder on this Mac (~ is expanded).")],
    remote_dir: Annotated[str, Field(description="Destination directory on the device.")] = "/sdcard/Download/",
) -> str:
    """Copy a file (or folder) from this Mac to the device, then run a media scan so it shows up in
    gallery and file pickers immediately.

    If a file with the same name already exists on the device, the copy is saved as "name (1).ext"
    instead of overwriting it.
    """
    src = Path(local_path).expanduser()
    if not src.exists():
        raise ToolError(f"Local path not found: {src}")
    if not remote_dir.startswith("/"):
        raise ToolError("remote_dir must be an absolute device path, such as /sdcard/Download/")
    serial = _serial()
    dest = _unique_remote_path(serial, remote_dir.rstrip("/") + "/" + src.name)
    proc = adb.run(["push", str(src), dest], serial=serial, timeout=900)
    out = adb.decode(proc.stdout + proc.stderr)
    result: dict[str, Any] = {"pushed": str(src), "to": dest, "adb": out.splitlines()[-1] if out else ""}
    result["media_scan"] = _media_scan(serial, dest, src.is_dir())
    return _json(result)


@tool()
def pull_file(
    remote_path: Annotated[str, Field(description="File or folder on the device.")],
    local_dir: Annotated[str, Field(description="Destination folder on this Mac (~ is expanded).")] = "~/Downloads",
) -> str:
    """Copy a file (or folder) from the device to a folder on this Mac.

    Never overwrites: if the name is taken locally, the copy is saved as "name (1).ext".
    """
    serial = _serial()
    if not _remote_exists(serial, remote_path):
        raise ToolError(f"{remote_path} does not exist on the device. Use list_files to browse.")
    target_dir = Path(local_dir).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    name = PurePosixPath(remote_path.rstrip("/")).name or "pulled"
    dest = _unique_local_path(target_dir / name)
    proc = adb.run(["pull", remote_path, str(dest)], serial=serial, timeout=900)
    out = adb.decode(proc.stdout + proc.stderr)
    result: dict[str, Any] = {"pulled": remote_path, "to": str(dest), "adb": out.splitlines()[-1] if out else ""}
    if dest.is_file():
        result["bytes"] = dest.stat().st_size
    return _json(result)


@tool(read_only=True)
def list_files(
    remote_dir: Annotated[str, Field(description="Directory on the device.")] = "/sdcard/Download/",
) -> str:
    """List a device directory (`ls -la`): name, type (file/dir/link), size in bytes, modification time."""
    serial = _serial()
    path = remote_dir if remote_dir.endswith("/") else remote_dir + "/"
    out = _sh(serial, f"ls -la {q(path)}")
    entries = []
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8 or line.startswith("total"):
            continue
        perms, _links, _owner, _group, size, date, clock, name = parts
        if name in (".", ".."):
            continue
        entry: dict[str, Any] = {"name": name, "type": {"d": "dir", "l": "link"}.get(perms[0], "file")}
        if entry["type"] == "link" and " -> " in name:
            entry["name"], entry["target"] = name.split(" -> ", 1)
        entry["size"] = int(size) if size.isdigit() else size
        entry["modified"] = f"{date} {clock}"
        entries.append(entry)
    return _json({"dir": path, "count": len(entries), "entries": entries})


# --------------------------------------------------------------------------- escape hatch


@tool(destructive=True)
def shell(
    command: Annotated[str, Field(description="Command line to run in the device shell.")],
    timeout: Annotated[float, Field(description="Seconds before the command is abandoned.", gt=0, le=600)] = 30,
) -> str:
    """Run `adb shell <command>` on the selected device and return exit code, stdout and stderr.

    Disabled unless the server was started with ANDROID_MCP_ALLOW_SHELL=1. This is the only tool
    that can delete or modify arbitrary files on the device, so prefer the dedicated tools.
    """
    if os.environ.get("ANDROID_MCP_ALLOW_SHELL") != "1":
        raise ToolError(
            "shell() is disabled. To enable it, set ANDROID_MCP_ALLOW_SHELL=1 in this MCP server's "
            "environment and restart the server."
        )
    serial = _serial()
    proc = adb.run(["shell", command], serial=serial, timeout=timeout, check=False)
    limit = 20_000
    stdout, stderr = adb.decode(proc.stdout), adb.decode(proc.stderr)
    return _json(
        {
            "exit_code": proc.returncode,
            "stdout": stdout if len(stdout) <= limit else stdout[:limit] + "\n...[truncated]",
            "stderr": stderr if len(stderr) <= limit else stderr[:limit] + "\n...[truncated]",
        }
    )


def main() -> None:
    """Entry point for `android-mcp`: serve over stdio."""
    _write_log(f"{datetime.now().astimezone().isoformat(timespec='milliseconds')} | server start | pid {os.getpid()}")
    mcp.run()


if __name__ == "__main__":
    main()
