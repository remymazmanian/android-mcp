# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub: open the [Security tab](https://github.com/remymazmanian/android-mcp/security) and choose **Report a vulnerability**. Don't open a public issue for a vulnerability.

Include your macOS, Python and Android emulator versions, what you found and, if you can, steps that reproduce it. You'll get a reply within a few days, and fixes are released as soon as they're ready.

## Supported versions

Security fixes go into the latest release.

## Scope

Especially welcome:

- ways to read or write Mac files outside the files folder (`ANDROID_MCP_FILES_DIR`, `~/android-mcp-files` by default) through `push_file`, `pull_file` or `install_apk`
- ways to run arbitrary commands on the Mac, or on the device while `shell` is off, for example by injecting into a device command line through a tool argument
- typed text or URL secrets that reach `logs/actions.log` or an error message unredacted
- ways to make the server act on a physical device that wasn't named in `ANDROID_SERIAL`

Problems in adb, the Android emulator or Android itself should go to Google.
