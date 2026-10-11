# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-08

First release of Android MCP, a local MCP server that lets an AI assistant or any other MCP client see and drive an Android emulator on your Mac through `adb`. It needs macOS with Android Studio, an Android Virtual Device and uv.

### Added

- Device tools to list devices, start an emulator and wait for it to finish booting, stop it, and read the model, Android version and screen size.
- Screenshots, downscaled with the scale reported alongside, and UI hierarchy dumps that give each element's tap-ready center coordinates.
- Input tools to tap by coordinates or by an element's text, description or resource ID, long press, swipe, scroll, type text, press keys, and wait for an element to appear.
- App tools to list installed packages, launch and force-stop apps, show the app in the foreground, install an APK and open a URL.
- File tools to push files to the device, pull files from it and list device folders. Pushed files are media-scanned so they show up in gallery pickers right away.
- Setup snippets for the Claude desktop app, Claude Code, Codex, LM Studio, VS Code (Copilot agent mode) and Continue.
- Activity log of every tool call, with typed text recorded only as `[redacted N chars]` and opened URLs trimmed to the scheme and host.
- File sandbox: file tools on the Mac only read from and write to one folder, `~/android-mcp-files` by default (set with `ANDROID_MCP_FILES_DIR`), and never delete or overwrite files there.
- Optional `shell` tool, off unless `ANDROID_MCP_ALLOW_SHELL=1` is set.
- Emulators only by default: a physical device is controlled only when you name it in `ANDROID_SERIAL`.

[Unreleased]: https://github.com/remymazmanian/android-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/remymazmanian/android-mcp/releases/tag/v0.1.0
