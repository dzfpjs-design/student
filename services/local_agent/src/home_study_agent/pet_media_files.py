"""Read bounded local bytes through pinned handles, never a second path open.

Windows: CreateFileW OPEN_REPARSE_POINT, directory handles held without write/
delete sharing, local drives only, then ReadFile on the verified file handle.
See Microsoft's CreateFileW and GetFileInformationByHandle documentation.
No Windows fallback to Path.read_bytes: inability to enforce this fails closed.
"""

import os
from pathlib import Path, PureWindowsPath
import stat
import re


def read_managed_media(local_path, *, media_root, max_bytes):
    if type(max_bytes) is not int or not 0 < max_bytes <= 24 * 1024 * 1024:
        raise ValueError("invalid_media_limit")
    if os.name == "nt":
        return _read_windows(str(local_path), str(media_root), max_bytes)
    return _read_posix(local_path, media_root, max_bytes)


def write_new_managed_media(content, *, media_root, filename):
    """Exclusive encrypted file creation under pinned existing root handles.

    Caller reserves the path in capture_artifact before this operation so crashes
    and privacy deletion retain an owner. No user-supplied path or overwrite.
    """
    if (not isinstance(content, bytes) or not content.startswith(b"HSC1")
            or not 32 <= len(content) <= 24 * 1024 * 1024
            or not re.fullmatch(r"[0-9a-f]{32}\.(?:wav|png)\.enc", filename)):
        raise ValueError("invalid_encrypted_media_creation")
    handles = []
    root = Path(media_root)
    if os.name == "nt":
        parts, kernel = _windows_parts(str(root)), _Win32()
        if kernel.drive_type(parts[0]) not in {2, 3}:
            raise OSError("nonlocal_media_drive")
        try:
            for index in range(len(parts)):
                path = str(PureWindowsPath(*parts[:index + 1]))
                handle = kernel.open(path, directory=True)
                handles.append(handle)
                info = kernel.info(handle)
                final = kernel.final_path(handle)
                if final.startswith("\\\\?\\"):
                    final = final[4:]
                if (info["file_type"] != 1 or info["attributes"] & (0x400 | 0x1000)
                        or not info["attributes"] & 0x10
                        or final.rstrip("\\").casefold() != path.rstrip("\\").casefold()):
                    raise OSError("media_reparse_offline_or_wrong_type")
            handle = kernel.create(str(PureWindowsPath(*parts, filename)))
            handles.append(handle)
            kernel.write(handle, content)
            kernel.flush(handle)
        finally:
            for handle in reversed(handles):
                kernel.close(handle)
    else:
        if (not root.is_absolute() or root == Path("/") or ".." in root.parts
                or os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW")):
            raise ValueError("managed_absolute_path_required")
        try:
            handles.append(os.open("/", os.O_RDONLY | os.O_DIRECTORY))
            for part in root.parts[1:]:
                handles.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handles[-1]))
            fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=handles[-1])
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(handles[-1])
        finally:
            for fd in reversed(handles):
                os.close(fd)
    return str(root / filename)


def _read_posix(local_path, root, limit):
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise OSError("safe_media_handles_unavailable")
    root, path = Path(root), Path(local_path)
    if not root.is_absolute() or not path.is_absolute() or root == Path("/"):
        raise ValueError("managed_absolute_path_required")
    if any(part in {".", ".."} for part in str(local_path).split("/")):
        raise ValueError("ambiguous_media_path")
    relative = path.relative_to(root)
    if not relative.parts:
        raise ValueError("media_file_required")
    handles = []
    try:
        handles.append(os.open("/", os.O_RDONLY | os.O_DIRECTORY))
        for part in (*root.parts[1:], *relative.parts[:-1]):
            handles.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handles[-1]))
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=handles[-1])
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
                raise ValueError("invalid_media_file")
            content = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            if (len(content) != before.st_size or len(content) > limit
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError("media_changed_during_read")
            return content
    finally:
        for fd in reversed(handles):
            os.close(fd)


def _windows_parts(value):
    if not isinstance(value, str):
        raise ValueError("windows_local_path_required")
    value = value.replace("/", "\\")
    if (len(value) < 4 or value[1:3] != ":\\" or value[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            or any(ord(c) < 32 for c in value)):
        raise ValueError("windows_local_path_required")
    parts = value[3:].split("\\")
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(prefix + suffix for prefix in ("COM", "LPT") for suffix in (*"123456789", "\u00b9", "\u00b2", "\u00b3"))
    if any(not p or p in {".", ".."} or p.endswith((" ", ".")) or any(c in p for c in ':<>"|?*')
           or p.split(".")[0].upper() in reserved for p in parts):
        raise ValueError("ambiguous_windows_media_path")
    return [value[0].upper() + ":\\", *parts]


def _read_windows(local_path, root, limit, *, kernel=None):
    source_parts, root_parts = _windows_parts(local_path), _windows_parts(root)
    if (len(source_parts) <= len(root_parts)
            or [p.casefold() for p in source_parts[:len(root_parts)]] != [p.casefold() for p in root_parts]):
        raise ValueError("media_outside_root")
    # Build from the SERVER root, not the request's spelling. This also prevents
    # case-sensitive NTFS sibling directories from bypassing root containment.
    parts = root_parts + source_parts[len(root_parts):]
    kernel = kernel or _Win32()
    if kernel.drive_type(parts[0]) not in {2, 3}:
        raise OSError("nonlocal_media_drive")
    handles = []
    try:
        for index in range(len(parts)):
            path = str(PureWindowsPath(*parts[:index + 1]))
            directory = index < len(parts) - 1
            handle = kernel.open(path, directory=directory)
            handles.append(handle)
            info = kernel.info(handle)
            if (info["file_type"] != 1 or info["attributes"] & (0x400 | 0x1000)
                    or bool(info["attributes"] & 0x10) != directory):
                raise OSError("media_reparse_offline_or_wrong_type")
            final_path = kernel.final_path(handle)
            if final_path.startswith("\\\\?\\"):
                final_path = final_path[4:]
            if final_path.rstrip("\\").casefold() != path.rstrip("\\").casefold():
                raise OSError("media_handle_path_changed")
        before = kernel.info(handles[-1])
        if before["links"] != 1 or not 0 <= before["size"] <= limit:
            raise ValueError("invalid_media_file")
        content = kernel.read(handles[-1], limit)
        if len(content) != before["size"] or len(content) > limit or kernel.info(handles[-1]) != before:
            raise OSError("media_changed_during_read")
        return content
    finally:
        for handle in reversed(handles):
            kernel.close(handle)


class _Win32:
    """Small native adapter; policy above is independently adversarially tested."""
    def __init__(self):
        import ctypes as ct

        self.ct = ct
        self.dll = ct.WinDLL("kernel32", use_last_error=True)
        DWORD, HANDLE = ct.c_uint32, ct.c_void_p

        class FileTime(ct.Structure):
            _fields_ = [("low", DWORD), ("high", DWORD)]

        class FileInfo(ct.Structure):
            _fields_ = [("attributes", DWORD), ("creation", FileTime), ("access", FileTime),
                        ("write", FileTime), ("volume", DWORD), ("size_high", DWORD),
                        ("size_low", DWORD), ("links", DWORD), ("index_high", DWORD), ("index_low", DWORD)]

        self.FileInfo = FileInfo
        signatures = {
            "CreateFileW": ([ct.c_wchar_p, DWORD, DWORD, HANDLE, DWORD, DWORD, HANDLE], HANDLE),
            "GetFileInformationByHandle": ([HANDLE, ct.POINTER(FileInfo)], ct.c_int),
            "GetFileType": ([HANDLE], DWORD),
            "GetFinalPathNameByHandleW": ([HANDLE, ct.c_wchar_p, DWORD, DWORD], DWORD),
            "GetDriveTypeW": ([ct.c_wchar_p], DWORD),
            "ReadFile": ([HANDLE, HANDLE, DWORD, ct.POINTER(DWORD), HANDLE], ct.c_int),
            "WriteFile": ([HANDLE, HANDLE, DWORD, ct.POINTER(DWORD), HANDLE], ct.c_int),
            "FlushFileBuffers": ([HANDLE], ct.c_int),
            "CloseHandle": ([HANDLE], ct.c_int),
        }
        for name, (args, result) in signatures.items():
            method = getattr(self.dll, name)
            method.argtypes, method.restype = args, result

    def drive_type(self, drive):
        return self.dll.GetDriveTypeW(drive)

    def create(self, path):
        handle = self.dll.CreateFileW("\\\\?\\" + path, 0x40000000, 0, None, 1,
                                      0x00200000 | 0x00100000, None)
        if handle == self.ct.c_void_p(-1).value:
            raise self.ct.WinError(self.ct.get_last_error())
        return handle

    def write(self, handle, content):
        count = self.ct.c_uint32()
        buffer = self.ct.create_string_buffer(content)
        if not self.dll.WriteFile(handle, buffer, len(content), self.ct.byref(count), None) or count.value != len(content):
            raise OSError("media_write_incomplete")

    def flush(self, handle):
        if not self.dll.FlushFileBuffers(handle):
            raise self.ct.WinError(self.ct.get_last_error())

    def open(self, path, *, directory):
        # FILE_SHARE_READ only: no concurrent writer, mapping writer or rename.
        handle = self.dll.CreateFileW("\\\\?\\" + path, 0x80 if directory else 0x80000000,
            1, None, 3, 0x00200000 | 0x00100000 | (0x02000000 if directory else 0), None)
        if handle == self.ct.c_void_p(-1).value:
            raise self.ct.WinError(self.ct.get_last_error())
        return handle

    def info(self, handle):
        info = self.FileInfo()
        if not self.dll.GetFileInformationByHandle(handle, self.ct.byref(info)):
            raise self.ct.WinError(self.ct.get_last_error())
        return dict(attributes=info.attributes, size=(info.size_high << 32) | info.size_low,
                    links=info.links, identity=(info.volume, info.index_high, info.index_low),
                    modified=(info.write.high << 32) | info.write.low, file_type=self.dll.GetFileType(handle))

    def final_path(self, handle):
        buffer = self.ct.create_unicode_buffer(32768)
        length = self.dll.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not 0 < length < len(buffer):
            raise OSError("media_final_path_unavailable")
        return buffer.value

    def read(self, handle, limit):
        chunks, size = [], 0
        while size <= limit:
            buffer = self.ct.create_string_buffer(min(65536, limit + 1 - size))
            count = self.ct.c_uint32()
            if not self.dll.ReadFile(handle, buffer, len(buffer), self.ct.byref(count), None):
                raise self.ct.WinError(self.ct.get_last_error())
            if not count.value:
                break
            chunks.append(buffer.raw[:count.value])
            size += count.value
        return b"".join(chunks)

    def close(self, handle):
        self.dll.CloseHandle(handle)
