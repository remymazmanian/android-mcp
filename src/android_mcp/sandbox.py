"""The one folder on the Mac that the file tools may read from and write to.

push_file, pull_file and install_apk only accept Mac paths inside this folder, so a
prompt-injected assistant can't copy e.g. ~/.ssh to the device or drop files anywhere else.
"""

import os
from pathlib import Path

FILES_DIR_ENV = "ANDROID_MCP_FILES_DIR"
DEFAULT_FILES_DIR = Path.home() / "android-mcp-files"


class SandboxError(ValueError):
    """A Mac path lies outside the allowed files folder."""


def files_root() -> Path:
    """The allowed folder ($ANDROID_MCP_FILES_DIR, else ~/android-mcp-files), resolved and created if missing."""
    root = Path(os.environ.get(FILES_DIR_ENV) or DEFAULT_FILES_DIR).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_inside(path: str | None, what: str) -> Path:
    """Resolve a Mac path and require it to be inside the files folder.

    `~` is expanded, relative paths are taken relative to the folder, None or "" means the
    folder itself, and symlinks are followed, so `../` tricks and links pointing outside fail.
    """
    root = files_root()
    candidate = root if not path else Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        shown = f"{candidate}" if str(candidate) == str(resolved) else f"{candidate} (resolves to {resolved})"
        raise SandboxError(
            f"{what} {shown} is outside the allowed files folder {root}. Move the file into that "
            f"folder, or set {FILES_DIR_ENV} to another folder and restart the server."
        )
    return resolved


def check_tree(folder: Path) -> None:
    """Reject a folder that contains symlinks leading outside the files folder."""
    root = files_root()
    for dirpath, dirnames, filenames in os.walk(folder):  # does not descend into symlinked dirs
        for name in dirnames + filenames:
            entry = Path(dirpath) / name
            if entry.is_symlink() and not entry.resolve().is_relative_to(root):
                raise SandboxError(
                    f"{entry} is a symlink to {entry.resolve()}, which is outside the allowed files folder {root}."
                )
