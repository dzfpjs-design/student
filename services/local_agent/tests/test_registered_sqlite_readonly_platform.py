"""Read-only SQLite contract and real Windows long-path/HeldFile regression.

Only fresh public synthetic temporary databases; no App, provider or family data.
"""
import hashlib
import os
from pathlib import Path, PureWindowsPath
import sqlite3
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from home_study_agent import registered_asset_snapshot as subject
from home_study_agent.backup_local_io import io_path, _windows_io_name


def database(tmp_path):
    path = tmp_path / 'public.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('PRAGMA journal_mode=DELETE')
        connection.execute('CREATE TABLE public_rows(value INTEGER)')
        connection.execute('INSERT INTO public_rows VALUES (7)')
    connection.close()
    return path


def digest(path):
    return hashlib.sha256(io_path(path).read_bytes()).hexdigest()


def test_real_sqlite_readonly_pragmas_write_rejection_and_close(tmp_path):
    path = database(tmp_path)
    before = digest(path)
    with subject._open_readonly(path) as connection:
        assert connection.execute('PRAGMA query_only').fetchone()[0] == 1
        assert connection.execute('PRAGMA trusted_schema').fetchone()[0] == 0
        assert connection.execute('SELECT value FROM public_rows').fetchone()[0] == 7
        with pytest.raises(sqlite3.OperationalError):
            connection.execute('INSERT INTO public_rows VALUES (8)')
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute('SELECT 1')
    assert digest(path) == before
    assert not any(io_path(Path(str(path) + suffix)).exists() for suffix in ('-wal', '-shm', '-journal'))


def test_real_body_error_preserves_original_exception_and_closes(tmp_path):
    path = database(tmp_path)
    error = RuntimeError('public synthetic body failure')
    with pytest.raises(RuntimeError) as raised:
        with subject._open_readonly(path) as connection:
            raise error
    assert raised.value is error
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute('SELECT 1')


def test_missing_database_readonly_open_never_creates(tmp_path):
    missing = tmp_path / 'not-created.sqlite3'
    with pytest.raises(sqlite3.Error):
        with subject._open_readonly(missing):
            pytest.fail('missing database was opened')
    assert not io_path(missing).exists()


class Connection:
    def __init__(self, close_error=None):
        self.commands = []
        self.closes = 0
        self.close_error = close_error
    def execute(self, command):
        self.commands.append(command)
    def close(self):
        self.closes += 1
        if self.close_error is not None:
            raise self.close_error


def windows_branch(monkeypatch, connection):
    calls = []
    def connect(database_name, **kwargs):
        calls.append((database_name, kwargs))
        return connection
    monkeypatch.setattr(subject, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(subject, 'sqlite3', SimpleNamespace(connect=connect, Row=sqlite3.Row))
    return calls


def test_windows_uri_encoding_cannot_become_authority_or_query(monkeypatch):
    connection = Connection()
    calls = windows_branch(monkeypatch, connection)
    path = PureWindowsPath('C:/public/space #percent% amp& 中文.sqlite3')
    with subject._open_readonly(path):
        pass
    assert len(calls) == 1
    uri, kwargs = calls[0]
    parsed = urlsplit(uri)
    assert parsed.scheme == 'file' and parsed.netloc == ''
    assert unquote(parsed.path) == _windows_io_name(path)
    assert parse_qs(parsed.query) == {'mode': ['ro'], 'vfs': ['win32-longpath']}
    assert kwargs == {'uri': True, 'timeout': 0}
    assert connection.commands == ['PRAGMA query_only=ON', 'PRAGMA trusted_schema=OFF']
    assert connection.closes == 1


@pytest.mark.parametrize('path', [
    r'\\server\share\source.db', 'relative.db', r'\\?\C:\already-device.db',
    r'C:\public\source.db:alternate', r'C:\public\..\source.db', r'C:\public\CON.db',
])
def test_windows_nonlocal_or_ambiguous_names_fail_before_connect(monkeypatch, path):
    calls = windows_branch(monkeypatch, Connection())
    with pytest.raises(ValueError):
        with subject._open_readonly(path):
            pytest.fail('invalid path admitted')
    assert calls == []


def test_original_close_failure_is_not_swallowed_or_retried(monkeypatch):
    error = RuntimeError('public synthetic close failure')
    connection = Connection(error)
    calls = windows_branch(monkeypatch, connection)
    with pytest.raises(RuntimeError) as raised:
        with subject._open_readonly(PureWindowsPath('C:/public/source.db')):
            pass
    assert raised.value is error and connection.closes == 1 and len(calls) == 1


@pytest.mark.skipif(os.name != 'nt', reason='real Windows VFS and native HeldFile regression')
def test_real_windows_260_path_under_original_held_guard(tmp_path):
    source = database(tmp_path)
    basename = 'public #percent% 中文.sqlite3'
    parent = tmp_path / 'long'
    padding = 260 - len(str(parent)) - len(basename) - 2
    assert 1 <= padding <= 200
    parent = parent / ('p' * padding)
    io_path(parent).mkdir(parents=True)
    target = parent / basename
    io_path(target).write_bytes(io_path(source).read_bytes())
    assert len(str(target)) == 260
    before = digest(target)
    policy = subject.Policy(4, 64, 1024, 1024*1024, 1024*1024, 2*1024*1024)
    with subject.HeldFile(target.parent, target.name, policy) as held:
        with subject._open_readonly(target) as connection:
            assert connection.execute('SELECT value FROM public_rows').fetchone()[0] == 7
            assert connection.execute('PRAGMA query_only').fetchone()[0] == 1
        held.check()
        assert held.hash()[0] == before
    assert digest(target) == before
    assert not any(io_path(Path(str(target) + suffix)).exists() for suffix in ('-wal', '-shm', '-journal'))
