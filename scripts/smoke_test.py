"""End-to-end smoke test for android-mcp. Needs a running emulator.

    uv --directory ~/agent-tools/android-mcp run python scripts/smoke_test.py

Starts the real server over stdio (the same way an MCP client does) and drives it through:
list devices -> screenshot -> UI dump -> home -> open Settings -> tap "Network & internet"
-> back. Exits non-zero on the first failure.
"""

import asyncio
import base64
import json
import sys
import time
from pathlib import Path

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
ENTRY_POINT = Path(sys.executable).with_name("android-mcp")
SERVER = StdioServerParameters(
    command=str(ENTRY_POINT) if ENTRY_POINT.exists() else sys.executable,
    args=[] if ENTRY_POINT.exists() else ["-m", "android_mcp"],
    cwd=str(ROOT),
)
SETTINGS = "com.android.settings"


class SmokeTestFailure(Exception):
    pass


async def call(client: Client, name: str, args: dict | None = None) -> tuple[str, list]:
    started = time.monotonic()
    result = await client.call_tool(name, args or {})
    text = "\n".join(c.text for c in result.content if c.type == "text")
    images = [c for c in result.content if c.type == "image"]
    shown = text if len(text) <= 160 else text[:157] + "..."
    print(f"  {name}({json.dumps(args or {})}) {time.monotonic() - started:.1f}s -> {shown}")
    if result.is_error:
        raise SmokeTestFailure(f"{name} returned an error: {text}")
    return text, images


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeTestFailure(message)


async def main() -> int:
    log_file = ROOT / "logs/actions.log"
    log_lines_before = len(log_file.read_text().splitlines()) if log_file.exists() else 0

    async with Client(SERVER, read_timeout_seconds=120) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        print(f"Connected: {len(tools)} tools")

        print("1. list_devices")
        text, _ = await call(client, "list_devices")
        selected = json.loads(text)["selected"]
        check(selected is not None, "no device selected; is the emulator running?")

        print("2. screenshot")
        text, images = await call(client, "screenshot", {"scale": 0.5})
        meta = json.loads(text)
        check(len(images) == 1, "screenshot returned no image")
        data = base64.b64decode(images[0].data)
        out = ROOT / "logs/smoke_screenshot.jpg"
        out.write_bytes(data)
        check(len(data) > 1000, "screenshot image is suspiciously small")
        check(meta["image_size"][0] * 2 == meta["full_resolution"][0], f"unexpected scale metadata: {meta}")
        print(f"     saved {out.relative_to(ROOT)} ({len(data) // 1024} KB, {meta['image_size']} of {meta['full_resolution']})")

        print("3. ui_dump")
        text, _ = await call(client, "ui_dump")
        dump = json.loads(text)
        check(dump["count"] > 0 and len(dump["elements"]) == dump["count"], "ui_dump returned no elements")

        print("4. home")
        await call(client, "press_key", {"key": "home"})

        print("5. open Settings (force-stopped first so it opens on its main page)")
        await call(client, "force_stop", {"package": SETTINGS})
        text, _ = await call(client, "launch_app", {"package": SETTINGS})
        check(json.loads(text)["foreground"]["package"] == SETTINGS, "Settings is not in the foreground")

        print('6. find and tap "Network & internet"')
        await call(client, "wait_for_element", {"text": "Network & internet", "timeout": 15})
        text, _ = await call(client, "tap_element", {"text": "Network & internet"})
        await call(client, "wait_for_element", {"text": "Airplane mode", "timeout": 10})

        print("7. back")
        await call(client, "press_key", {"key": "back"})
        await call(client, "wait_for_element", {"text": "Connected devices", "timeout": 10})

    log_lines_after = len(log_file.read_text().splitlines()) if log_file.exists() else 0
    check(log_lines_after > log_lines_before, "logs/actions.log did not grow")
    print(f"actions.log: +{log_lines_after - log_lines_before} lines")
    return 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    except SmokeTestFailure as e:
        print(f"\nFAIL: {e}")
        sys.exit(1)
    print("\nPASS")
    sys.exit(code)
