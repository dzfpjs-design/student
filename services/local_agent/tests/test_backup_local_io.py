"""OS spelling is separate from canonical namespace and backup authority."""
import os
from pathlib import Path
import shutil

import pytest

from home_study_agent.backup_local_io import (
    _canonical_windows_name, _logical_result, _windows_io_name, io_glob, io_mkdtemp, io_path, io_resolve,
    io_temporary_directory, io_walk,
)


@pytest.mark.parametrize('value', [r'C:\owned\one.enc', r'd:\公开\原始.txt',
                                   'C:\\owned\\' + 'a' * 240 + '\\' + 'b' * 64 + '.enc'])
def test_strict_local_spelling_preserves_original_components(value):
    assert _windows_io_name(value) == '\\\\?\\' + value


@pytest.mark.parametrize('value', [
    r'\\server\share\file', r'\\?\C:\owned\file', r'\\.\C:\owned\file',
    r'C:relative', r'\rooted', '/posix/path', 'C:\\',
    r'C:\owned\..\file', r'C:\owned\.\file', r'C:\owned\\file',
    'C:\\owned\\name.', 'C:\\owned\\name ', r'C:\owned\file:stream',
    r'C:\owned\CON', r'C:\owned\nul.txt', r'C:\owned\COM1.log',
    'C:\\owned\\control\x00name',
])
def test_original_windows_local_rules_reject_ambiguous_or_foreign_namespace(value):
    with pytest.raises(ValueError):
        _windows_io_name(value)


def test_enumeration_result_rejoins_original_logical_parent_without_prefix():
    parent = r'C:\Owned\数据'
    actual = '\\\\?\\C:\\Owned\\数据\\generation\\one.enc'
    assert _logical_result(parent, actual) == r'C:\Owned\数据\generation\one.enc'
    assert _logical_result(parent, '\\\\?\\C:\\Owned\\数据') == parent


def test_resolved_local_alias_is_returned_canonical_for_original_guard_to_reject():
    assert _canonical_windows_name(r'\\?\D:\outside\one.enc') == r'D:\outside\one.enc'


@pytest.mark.parametrize('value', [r'\\?\UNC\server\share\one', r'\\.\C:\owned\one',
                                   r'\\?\\\?\C:\owned\one', r'\\?\C:\owned\..\one'])
def test_resolved_result_never_introduces_foreign_or_ambiguous_namespace(value):
    with pytest.raises(ValueError):
        _canonical_windows_name(value)


@pytest.mark.parametrize('actual', [
    r'\\?\C:\elsewhere\one.enc', r'\\?\C:\owned-lookalike\one.enc',
    r'\\?\C:\owned\..\escape', r'\\?\C:\owned\.\same',
    r'\\?\C:\owned\trailing.', r'\\?\C:\owned\NUL',
    r'\\?\UNC\server\share\one.enc', r'\\.\C:\owned\one.enc',
])
def test_enumeration_does_not_normalize_unsafe_tail_or_broaden_parent(actual):
    with pytest.raises(ValueError):
        _logical_result(r'C:\owned', actual)


@pytest.mark.skipif(os.name == 'nt', reason='Actual POSIX identity contract')
def test_posix_io_path_returns_the_same_input_object(tmp_path):
    for value in (tmp_path, str(tmp_path), 12):
        assert io_path(value) is value
    assert io_resolve(tmp_path, strict=True) == tmp_path.resolve(strict=True)


@pytest.mark.skipif(os.name == 'nt', reason='Actual POSIX alias observation')
def test_resolve_preserves_external_alias_for_original_relative_guard(tmp_path):
    owned = tmp_path / 'owned'; owned.mkdir()
    outside = tmp_path / 'outside'; outside.mkdir()
    (outside / 'one').write_text('public')
    link = owned / 'link'; link.symlink_to(outside, target_is_directory=True)
    resolved = io_resolve(link / 'one', strict=True)
    assert resolved == outside / 'one'
    with pytest.raises(ValueError):
        resolved.relative_to(owned)


@pytest.mark.skipif(os.name == 'nt', reason='Actual POSIX filesystem semantics')
def test_posix_walk_keeps_canonical_paths_and_original_pruning_lists(tmp_path):
    (tmp_path / 'keep').mkdir(); (tmp_path / 'prune').mkdir()
    (tmp_path / 'keep' / 'one').write_text('public')
    walker = io_walk(tmp_path)
    root, directories, files = next(walker)
    assert root == str(tmp_path)
    directories.remove('prune')
    assert list(walker) == [(str(tmp_path / 'keep'), [], ['one'])]
    assert list(io_glob(tmp_path, 'keep')) == [tmp_path / 'keep']


@pytest.mark.skipif(os.name == 'nt', reason='Actual POSIX temporary ownership')
def test_posix_temporary_paths_remain_logical_and_cleanup_is_original(tmp_path):
    created = Path(io_mkdtemp(dir=tmp_path, prefix='public-'))
    assert created.parent == tmp_path and created.is_dir()
    shutil.rmtree(created)
    with io_temporary_directory(dir=tmp_path, prefix='public-') as name:
        inside = Path(name)
        assert inside.parent == tmp_path and inside.is_dir()
        (inside / 'one').write_text('public')
    assert not inside.exists()


def _strict_actual_receiver_spy(module, monkeypatch):
    original = module.io_path
    seen = []
    def checked(value):
        if type(value) is int:
            os.fstat(value)  # Original POSIX scandir(fd), never a fake token.
        elif not isinstance(value, (str, Path)):
            raise TypeError('IO_CALLER_NON_PATH_RECEIVER')
        seen.append(type(value).__name__)
        return original(value)
    monkeypatch.setattr(module, 'io_path', checked)
    return seen


@pytest.mark.skipif(os.name == 'nt', reason='Actual POSIX held-object caller seam')
def test_actual_owned_generation_keeps_capability_receiver(tmp_path, monkeypatch):
    import hashlib
    from home_study_agent import registered_asset_restore as restore
    from home_study_agent import registered_asset_snapshot as snapshot
    _strict_actual_receiver_spy(restore, monkeypatch)
    _strict_actual_receiver_spy(snapshot, monkeypatch)
    source = tmp_path / 'source'; source.mkdir(mode=0o700)
    target = tmp_path / 'target'; target.mkdir(mode=0o700)
    content = b'public actual held source'
    (source / 'one').write_bytes(content)
    policy = snapshot.Policy(100, 64, 1024, 8192, 65536, 65536)
    with snapshot.HeldFile(source, 'one', policy) as held:
        with restore._OwnedGeneration(target, policy) as generation:
            generation.copy(held, 'nested/one', {'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
            generation.validate_namespace({'nested/one'})
            generation.flush()
            assert (generation.work / 'nested/one').read_bytes() == content
    assert list(target.iterdir()) == []


@pytest.mark.skipif(os.name == 'nt', reason='Actual POSIX original DirEntry metadata seam')
def test_actual_registered_scan_keeps_original_direntry_receiver(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from home_study_agent import registered_asset_snapshot as snapshot
    seen = _strict_actual_receiver_spy(snapshot, monkeypatch)
    source = tmp_path / 'source'; source.mkdir(mode=0o700)
    (source / 'one').write_bytes(b'public original metadata')
    policy = snapshot.Policy(100, 64, 1024, 8192, 65536, 65536)
    collector = SimpleNamespace(visited=0)
    assert list(snapshot._registered_files(source, policy, collector)) == ['one']
    assert collector.visited == 1
    assert 'DirEntry' not in seen


def test_only_windows_absolute_open_is_adapted_and_capability_calls_stay_original():
    import ast
    import inspect
    import textwrap
    from home_study_agent import registered_asset_restore as restore
    tree = ast.parse(textwrap.dedent(inspect.getsource(restore._OwnedGeneration)))
    derived = next(n for n in tree.body[0].body if isinstance(n, ast.FunctionDef) and n.name == 'derived_file')
    derived_nodes = set(ast.walk(derived))
    derived_mkdir = [n for n in ast.walk(derived) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                     and n.func.value.id == 'self' and n.func.attr == 'mkdir']
    assert [ast.unparse(n) for n in derived_mkdir] == ["self.mkdir('/'.join(parts[:-1]))"]
    # The original seven capability calls remain exact; the new preparation
    # method has its own held-parent contract rather than a relaxed total.
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and n not in derived_nodes]
    original_parent_calls = {}
    for method in tree.body[0].body:
        if isinstance(method, ast.FunctionDef) and method is not derived:
            entries = [ast.unparse(n) for n in ast.walk(method) if isinstance(n, ast.Call)
                       and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                       and n.func.value.id == 'self' and n.func.attr == 'mkdir']
            if entries:
                original_parent_calls[method.name] = entries
    assert original_parent_calls == {
        'validate_namespace': ['self.mkdir(relative)'],
        'copy': ["self.mkdir('/'.join(parts[:-1]))"],
        'move': ["self.mkdir('/'.join(b[:-1]))", "self.mkdir('/'.join(a[:-1]))"],
        'flush_file': ["self.mkdir('/'.join(parts[:-1]))"],
        'set_mtime': ["self.mkdir('/'.join(parts[:-1]))"],
        'flush': ['self.mkdir(key)'],
    }

    assert sum(isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
               and n.func.value.id == 'self' and n.func.attr == 'mkdir' for n in calls) == 7
    copy = next(n for n in tree.body[0].body if isinstance(n, ast.FunctionDef) and n.name == 'copy')
    windows = next(n for n in copy.body if isinstance(n, ast.If) and ast.unparse(n.test) == "os.name == 'nt'")
    opened = next(n for n in ast.walk(windows.body[0]) if isinstance(n, ast.Call) and ast.unparse(n.func) == 'os.open')
    assert ast.unparse(opened.args[0]) == 'io_path(self.work / relative)'
    posix = next(n for n in ast.walk(windows.orelse[0]) if isinstance(n, ast.Call) and ast.unparse(n.func) == 'os.open')
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'io_path'
                   for n in ast.walk(posix.args[0]))
    derived_windows = next(n for n in ast.walk(derived) if isinstance(n, ast.If)
                           and ast.unparse(n.test) == "os.name == 'nt'")
    derived_open = next(n for n in ast.walk(derived_windows.body[0])
                        if isinstance(n, ast.Call) and ast.unparse(n.func) == 'os.open')
    assert ast.unparse(derived_open.args[0]) == 'io_path(self.work / relative)'
    derived_posix = next(n for n in ast.walk(derived_windows.orelse[0])
                         if isinstance(n, ast.Call) and ast.unparse(n.func) == 'os.open')
    assert ast.unparse(derived_posix.args[0]) == 'parts[-1]'
    assert {k.arg: ast.unparse(k.value) for k in derived_posix.keywords} == {'dir_fd': 'parent'}
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'io_path'
                   for n in ast.walk(derived_posix.args[0]))

