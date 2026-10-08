"""Mac-side file tools only touch paths inside the allowed files folder."""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from android_mcp import sandbox, server

OUTSIDE = "outside the allowed files folder"


def assert_names_folder_and_env(error: pytest.ExceptionInfo, root) -> None:
    message = str(error.value)
    assert str(root) in message
    assert "ANDROID_MCP_FILES_DIR" in message


# ----------------------------------------------------------------------------- the folder itself


def test_files_root_comes_from_env_and_is_created(isolated, monkeypatch):
    target = isolated / "custom" / "nested"
    monkeypatch.setenv(sandbox.FILES_DIR_ENV, str(target))

    assert sandbox.files_root() == target.resolve()
    assert target.is_dir()


def test_files_root_defaults_when_env_is_unset(isolated, monkeypatch):
    monkeypatch.delenv(sandbox.FILES_DIR_ENV)

    assert sandbox.files_root() == (isolated / "default-files").resolve()


# ----------------------------------------------------------------------------- push_file

PUSH_ESCAPES = [
    pytest.param(lambda root, out: str(out / "id_rsa"), id="absolute-outside"),
    pytest.param(lambda root, out: str(root / ".." / "outside" / "id_rsa"), id="absolute-dotdot"),
    pytest.param(lambda root, out: "../outside/id_rsa", id="relative-dotdot"),
    pytest.param(lambda root, out: "sub/../../outside/id_rsa", id="nested-dotdot"),
    pytest.param(lambda root, out: "~/.ssh/id_rsa", id="home-ssh"),
]


@pytest.fixture
def scanned(device):
    """Canned replies that make push_file's media scan succeed immediately."""
    device.shell_replies["readlink -f"] = "/storage/emulated/0/Download/photo.jpg"
    device.shell_replies["content query"] = "Row: 0 _id=1"
    return device


@pytest.mark.parametrize("given", ["absolute", "relative"])
def test_push_file_accepts_a_file_inside(scanned, files_root, given):
    photo = files_root / "photo.jpg"
    photo.write_bytes(b"jpeg")

    result = json.loads(server.push_file(local_path=str(photo) if given == "absolute" else "photo.jpg"))

    assert scanned.calls("push") == [["push", str(photo), "/sdcard/Download/photo.jpg"]]
    assert result["media_scan"]["indexed_in_mediastore"] is True


def test_push_file_accepts_a_symlink_that_stays_inside(scanned, files_root):
    (files_root / "real.jpg").write_bytes(b"jpeg")
    (files_root / "photo.jpg").symlink_to(files_root / "real.jpg")

    server.push_file(local_path="photo.jpg")

    assert scanned.calls("push")[0][1] == str(files_root / "real.jpg")


@pytest.mark.parametrize("make_path", PUSH_ESCAPES)
def test_push_file_rejects_paths_outside(device, files_root, outside, make_path):
    with pytest.raises(ToolError, match=OUTSIDE) as error:
        server.push_file(local_path=make_path(files_root, outside))

    assert_names_folder_and_env(error, files_root)
    assert device.calls("push") == []


def test_push_file_rejects_a_symlink_pointing_outside(device, files_root, outside):
    (files_root / "innocent.jpg").symlink_to(outside / "id_rsa")

    with pytest.raises(ToolError, match=f"resolves to .*{OUTSIDE}"):
        server.push_file(local_path="innocent.jpg")
    assert device.calls("push") == []


def test_push_file_rejects_a_folder_containing_a_symlink_outside(device, files_root, outside):
    album = files_root / "album"
    album.mkdir()
    (album / "ok.jpg").write_bytes(b"jpeg")
    (album / "key").symlink_to(outside / "id_rsa")

    with pytest.raises(ToolError, match="is a symlink to"):
        server.push_file(local_path="album")
    assert device.calls("push") == []


# ----------------------------------------------------------------------------- pull_file


@pytest.fixture
def remote_exists(device):
    device.shell_replies["test -e"] = "yes"
    return device


def test_pull_file_defaults_to_the_files_folder(remote_exists, files_root):
    result = json.loads(server.pull_file(remote_path="/sdcard/DCIM/photo.jpg"))

    assert remote_exists.calls("pull") == [["pull", "/sdcard/DCIM/photo.jpg", str(files_root / "photo.jpg")]]
    assert result["to"] == str(files_root / "photo.jpg")


def test_pull_file_accepts_a_subfolder(remote_exists, files_root):
    server.pull_file(remote_path="/sdcard/DCIM/photo.jpg", local_dir="shots")

    assert remote_exists.calls("pull")[0][2] == str(files_root / "shots" / "photo.jpg")
    assert (files_root / "shots").is_dir()


@pytest.mark.parametrize(
    "make_dir",
    [
        pytest.param(lambda root, out: str(out), id="absolute-outside"),
        pytest.param(lambda root, out: str(root / ".." / "outside"), id="absolute-dotdot"),
        pytest.param(lambda root, out: "../outside", id="relative-dotdot"),
        pytest.param(lambda root, out: "~", id="home"),
        pytest.param(lambda root, out: "/", id="filesystem-root"),
    ],
)
def test_pull_file_rejects_destinations_outside(remote_exists, files_root, outside, make_dir):
    with pytest.raises(ToolError, match=OUTSIDE) as error:
        server.pull_file(remote_path="/sdcard/DCIM/photo.jpg", local_dir=make_dir(files_root, outside))

    assert_names_folder_and_env(error, files_root)
    assert remote_exists.calls("pull") == []


def test_pull_file_rejects_a_symlinked_folder_pointing_outside(remote_exists, files_root, outside):
    (files_root / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ToolError, match=OUTSIDE):
        server.pull_file(remote_path="/sdcard/DCIM/photo.jpg", local_dir="escape")
    assert remote_exists.calls("pull") == []


def test_pull_file_never_writes_through_a_dangling_symlink(remote_exists, files_root, outside):
    (files_root / "photo.jpg").symlink_to(outside / "planted.jpg")  # target does not exist yet

    server.pull_file(remote_path="/sdcard/DCIM/photo.jpg")

    assert remote_exists.calls("pull")[0][2] == str(files_root / "photo (1).jpg")


@pytest.mark.parametrize("remote", ["/sdcard/Download/..", "/", "."])
def test_pull_file_rejects_remote_paths_without_a_name(remote_exists, remote):
    with pytest.raises(ToolError, match="must name a file or folder"):
        server.pull_file(remote_path=remote)
    assert remote_exists.calls("pull") == []


# ----------------------------------------------------------------------------- install_apk


def test_install_apk_accepts_an_apk_inside(device, files_root):
    apk = files_root / "app.apk"
    apk.write_bytes(b"apk")
    device.run_replies["install"] = b"Performing Streamed Install\nSuccess"

    assert server.install_apk(path="app.apk") == "Installed app.apk"
    assert device.calls("install") == [["install", "-r", str(apk)]]


@pytest.mark.parametrize(
    "make_path",
    [
        pytest.param(lambda root, out: str(out / "evil.apk"), id="absolute-outside"),
        pytest.param(lambda root, out: str(root / ".." / "outside" / "evil.apk"), id="absolute-dotdot"),
        pytest.param(lambda root, out: "../outside/evil.apk", id="relative-dotdot"),
    ],
)
def test_install_apk_rejects_paths_outside(device, files_root, outside, make_path):
    with pytest.raises(ToolError, match=OUTSIDE) as error:
        server.install_apk(path=make_path(files_root, outside))

    assert_names_folder_and_env(error, files_root)
    assert device.calls("install") == []


def test_install_apk_rejects_a_symlink_pointing_outside(device, files_root, outside):
    (files_root / "app.apk").symlink_to(outside / "evil.apk")

    with pytest.raises(ToolError, match=OUTSIDE):
        server.install_apk(path="app.apk")
    assert device.calls("install") == []
