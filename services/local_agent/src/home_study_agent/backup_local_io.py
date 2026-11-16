"""OS spellings for already-authorized local backup paths, never authority.

Keep caller logical paths, containment/alias checks and serialized names intact.
Only the immediate Windows filesystem spelling receives the extended prefix.
"""
from contextlib import contextmanager
import os
from pathlib import Path, PureWindowsPath
import tempfile

from .pet_media_files import _windows_parts


def _windows_io_name(value):
    raw = os.fspath(value)
    _windows_parts(raw)  # Original strict local drive/component rules.
    return '\\\\?\\' + raw.replace('/', '\\')


def io_path(value):
    """POSIX returns the same object; Windows changes only OS spelling."""
    return Path(_windows_io_name(value)) if os.name == 'nt' else value


def _canonical_windows_name(value):
    raw = os.fspath(value)
    if raw.startswith('\\\\?\\'):
        raw = raw[4:]
    _windows_parts(raw)
    return raw


def io_resolve(parent, *args, **kwargs):
    if os.name != 'nt':
        return parent.resolve(*args, **kwargs)
    # Resolve through the OS spelling but return the original logical domain.
    # A resolved local alias outside the root remains visible to caller guards.
    return Path(_canonical_windows_name(io_path(parent).resolve(*args, **kwargs)))


def _logical_result(parent, actual):
    """Return only canonical names below the same known logical parent."""
    original = os.fspath(parent)
    prefix = _windows_io_name(parent)
    value = os.fspath(actual)
    if value.casefold() == prefix.casefold():
        return original
    if not value.casefold().startswith(prefix.casefold() + '\\'):
        raise ValueError('backup_io_result_outside_parent')
    _windows_parts(value[4:])  # Reject ambiguous components before any joining.
    tail = value[len(prefix) + 1:]
    result = str(PureWindowsPath(original).joinpath(*tail.split('\\')))
    _windows_parts(result)
    return result


def io_walk(parent, *args, **kwargs):
    if os.name != 'nt':
        yield from os.walk(parent, *args, **kwargs)
        return
    for actual, directories, files in os.walk(io_path(parent), *args, **kwargs):
        # Preserve the original mutable directory lists and pruning semantics.
        yield _logical_result(parent, actual), directories, files


def io_glob(parent, pattern):
    if os.name != 'nt':
        yield from parent.glob(pattern)
        return
    for actual in io_path(parent).glob(pattern):
        yield Path(_logical_result(parent, actual))


def io_mkdtemp(*args, dir, **kwargs):
    actual = tempfile.mkdtemp(*args, dir=io_path(dir), **kwargs)
    return _logical_result(dir, actual) if os.name == 'nt' else actual


@contextmanager
def io_temporary_directory(*args, dir, **kwargs):
    # TemporaryDirectory retains its own I/O spelling for original cleanup.
    with tempfile.TemporaryDirectory(*args, dir=io_path(dir), **kwargs) as actual:
        yield _logical_result(dir, actual) if os.name == 'nt' else actual
