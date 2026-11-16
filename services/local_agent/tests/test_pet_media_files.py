"""Win32 contract tests run everywhere; native handle tests require Windows."""

import os
import ctypes
import subprocess
from types import SimpleNamespace

import pytest

from home_study_agent.pet_media_files import read_managed_media, _read_windows, _Win32


class Kernel:
    def __init__(self):
        self.live = []
        self.closed = []
        self.paths = {}
        self.metadata = {}
        self.drive = 3
        self.fail_at = None

    def drive_type(self, drive):
        return self.drive

    def open(self, path, *, directory):
        if path == self.fail_at:
            raise OSError("sharing violation")
        handle = len(self.paths) + 1
        self.paths[handle] = (path, directory)
        self.live.append(handle)
        return handle

    def info(self, handle):
        path, directory = self.paths[handle]
        return dict(attributes=16 if directory else 0, size=3, links=1,
                    identity=(1, handle), modified=1, file_type=1, **self.metadata.get(path, {}))

    def final_path(self, handle):
        return "\\\\?\\" + self.paths[handle][0]

    def read(self, handle, limit):
        assert len(self.live) == 4  # Drive, managed root, subdirectory, file.
        assert limit == 10
        return b"wav"

    def close(self, handle):
        self.live.remove(handle)
        self.closed.append(handle)


def test_windows_holds_entire_root_chain_until_exact_handle_read():
    kernel = Kernel()
    assert _read_windows(r"c:\MEDIA\sub\a.wav", r"C:\media", 10, kernel=kernel) == b"wav"
    assert list(kernel.paths.values()) == [("C:\\", True), (r"C:\media", True),
                                          (r"C:\media\sub", True), (r"C:\media\sub\a.wav", False)]
    assert kernel.live == [] and kernel.closed == [4, 3, 2, 1]


@pytest.mark.parametrize("path", [r"C:\media-other\a.wav", r"D:\media\a.wav", r"C:media\a.wav",
    r"\\host\media\a.wav", r"\\?\C:\media\a.wav", r"\\.\C:\media\a.wav", r"C:\media\..\a.wav",
    r"C:\media\.\a.wav", r"C:\media\a.wav:secret", r"C:\media\NUL.wav", r"C:\media\COM1",
    r"C:\media\a.wav.", "C:\\media\\a.wav ", "C:\\media\\a\x00.wav", r"C:\media\sub\..\a.wav"])
def test_windows_ambiguous_or_external_paths_fail_before_any_open(path):
    kernel = Kernel()
    with pytest.raises((OSError, ValueError)):
        _read_windows(path, r"C:\media", 10, kernel=kernel)
    assert kernel.paths == {}


@pytest.mark.parametrize("target,field,value", [
    (r"C:\media", "attributes", 16 | 1024),
    (r"C:\media\sub", "attributes", 16 | 1024),
    (r"C:\media\sub\a.wav", "attributes", 1024),
    (r"C:\media\sub\a.wav", "attributes", 4096),
    (r"C:\media\sub\a.wav", "links", 2),
    (r"C:\media\sub\a.wav", "size", 11),
    (r"C:\media\sub\a.wav", "file_type", 3)])
def test_windows_reparse_offline_hardlink_pipe_and_size_rejected(target, field, value):
    kernel = Kernel()
    original = kernel.info
    kernel.info = lambda handle: {**original(handle), **({field: value} if kernel.paths[handle][0] == target else {})}
    with pytest.raises((OSError, ValueError)):
        _read_windows(r"C:\media\sub\a.wav", r"C:\media", 10, kernel=kernel)
    assert kernel.live == []


def test_windows_remote_mapped_drive_never_opens():
    kernel = Kernel()
    kernel.drive = 4
    with pytest.raises((OSError, ValueError)):
        _read_windows(r"C:\media\sub\a.wav", r"C:\media", 10, kernel=kernel)
    assert kernel.paths == {}


def test_windows_sharing_violation_closes_ancestor_handles_and_never_falls_back():
    kernel = Kernel()
    kernel.fail_at = r"C:\media\sub\a.wav"
    with pytest.raises(OSError):
        _read_windows(r"C:\media\sub\a.wav", r"C:\media", 10, kernel=kernel)
    assert kernel.closed == [3, 2, 1]


def test_windows_unexpected_final_handle_path_rejected():
    kernel = Kernel()
    kernel.final_path = lambda handle: r"\\?\C:\elsewhere\a.wav"
    with pytest.raises((OSError, ValueError)):
        _read_windows(r"C:\media\sub\a.wav", r"C:\media", 10, kernel=kernel)
    assert kernel.live == []


def test_native_adapter_requests_nonwriting_nonrenamable_reparse_handles(monkeypatch):
    class Function:
        def __init__(self):
            self.calls = []

        def __call__(self, *args):
            self.calls.append(args)
            return 123

    dll = SimpleNamespace(**{name: Function() for name in ("CreateFileW", "GetFileInformationByHandle",
        "GetFileType", "GetFinalPathNameByHandleW", "GetDriveTypeW", "ReadFile", "CloseHandle")})
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs: dll, raising=False)
    dll.WriteFile = SimpleNamespace(argtypes=None, restype=None)
    dll.FlushFileBuffers = SimpleNamespace(argtypes=None, restype=None)
    native = _Win32()
    assert native.open(r"C:\media", directory=True) == 123
    native.open(r"C:\media\a.wav", directory=False)
    directory, file = dll.CreateFileW.calls
    assert directory == (r"\\?\C:\media", 0x80, 1, None, 3, 0x02300000, None)
    assert file == (r"\\?\C:\media\a.wav", 0x80000000, 1, None, 3, 0x00300000, None)
    assert ctypes.sizeof(native.FileInfo) == 52


def test_windows_changed_file_metadata_after_read_is_not_delivered():
    kernel = Kernel()
    original = kernel.read

    def changed(handle, limit):
        content = original(handle, limit)
        info = kernel.info
        kernel.info = lambda current: {**info(current), "modified": 2}
        return content

    kernel.read = changed
    with pytest.raises(OSError, match="changed"):
        _read_windows(r"C:\media\sub\a.wav", r"C:\media", 10, kernel=kernel)
    assert kernel.live == []


def test_native_file_read_and_limit(tmp_path):
    path = tmp_path / "a.wav"
    path.write_bytes(b"wav")
    assert read_managed_media(str(path), media_root=tmp_path, max_bytes=3) == b"wav"
    with pytest.raises((OSError, ValueError)):
        read_managed_media(str(path), media_root=tmp_path, max_bytes=2)


def test_native_hardlink_is_not_an_owned_media_copy(tmp_path):
    original = tmp_path / "outside.wav"
    original.write_bytes(b"wav")
    root = tmp_path / "managed"
    root.mkdir()
    os.link(original, root / "a.wav")
    with pytest.raises((OSError, ValueError)):
        read_managed_media(str(root / "a.wav"), media_root=root, max_bytes=10)


@pytest.mark.skipif(os.name != "nt", reason="Requires real Windows CreateFileW and junction semantics")
def test_native_windows_junction_cannot_escape_root(tmp_path):
    root, outside = tmp_path / "media", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "a.wav").write_bytes(b"wav")
    link = root / "junction"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
    assert result.returncode == 0
    with pytest.raises((OSError, ValueError)):
        read_managed_media(str(link / "a.wav"), media_root=root, max_bytes=10)
