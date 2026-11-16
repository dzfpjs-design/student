"""Main validator with simulated Windows cached fields and native held checks.

Cached zero link/dev/ino fields must not reject an ordinary file or grant it
permission: the unchanged HeldFile checks actual single-link native identity.
POSIX-backed simulation runs only on POSIX; fake-kernel native checks are portable.
These source controls do not claim a real Windows execution.
"""
import os
from pathlib import Path
import stat
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from home_study_agent import registered_asset_snapshot as snapshot
from home_study_agent import registered_asset_restore as restore

def exercise(tmp_path,*,hardlink=False,changed=False,unknown=False,reparse=False,directory_changed=False):
    work=tmp_path/'work';work.mkdir();path=work/'one.txt';path.write_bytes(b'public synthetic leaf')
    if hardlink:os.link(path,tmp_path/'true-alias.txt')
    source=os.stat(path,follow_symlinks=False)
    entry=SimpleNamespace(name='one.txt',stat=lambda **kw:SimpleNamespace(st_mode=source.st_mode,st_nlink=0,st_dev=0,st_ino=0,st_file_attributes=0x400 if reparse else 0))
    class Entries:
        def __enter__(self):return iter([entry])
        def __exit__(self,*args):pass
    active=[];closed=[];calls=[];checks=[]
    class Held:
        def __init__(self,root,relative,policy):
            calls.append((root,relative))
            with patch.object(os,'name','posix'):self.inner=snapshot.HeldFile(root,relative,policy)
        def __enter__(self):
            with patch.object(os,'name','posix'):self.inner.__enter__()
            active.append(self)
            if changed:
                before=path.stat();os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns+1000))
            return self
        def check(self):
            with patch.object(os,'name','posix'):self.inner.check()
        def __exit__(self,*args):
            try:return self.inner.__exit__(*args)
            finally:active.remove(self);closed.append(self)
    before=work.stat()
    def assert_dir(relative):
        checks.append(relative)
        now=work.stat()
        assert (before.st_dev,before.st_ino)==(now.st_dev,now.st_ino)
        if directory_changed and len(checks)==2:raise restore.RegisteredAssetRestoreError('REGISTERED_RESTORE_TARGET_ALIAS')
    g=SimpleNamespace(work=work,policy=snapshot.Policy(100,64,1024,1024,1024,1024),identities={'':(before.st_dev,before.st_ino)},
                      check=lambda:None,mkdir=lambda _:1,assert_dir=assert_dir,release_dirs=lambda:None)
    expected={'absent.txt'}if unknown else{'one.txt'}
    def simulated_io_path(value):
        # This cached-stat model uses an actual POSIX work directory.
        # Windows path spelling is verified by the independent backup I/O tests.
        assert value == work
        return value
    try:
        with patch.object(os,'name','nt'),patch.object(restore,'io_path',simulated_io_path),patch.object(os,'scandir',lambda _:Entries()),patch.object(restore,'HeldFile',Held):
            restore._OwnedGeneration.validate_namespace(g,expected)
    finally:
        assert active==[]
        if not hardlink and not unknown and not reparse:assert len(calls)==len(closed)==1
    return calls,checks


@pytest.mark.skipif(os.name=='nt',reason='POSIX-backed simulation of Windows cached fields')
def test_main_uses_original_held_chain_for_cached_zero_metadata(tmp_path):
    calls,checks=exercise(tmp_path)
    assert calls==[(tmp_path/'work','one.txt')]
    assert checks==['','']


@pytest.mark.skipif(os.name=='nt',reason='POSIX-backed simulation of Windows cached fields')
@pytest.mark.parametrize('fault',['hardlink','changed','unknown','reparse','directory_changed'])
def test_main_never_weakens_alias_and_namespace_guards(tmp_path,fault):
    with pytest.raises((snapshot.AssetSnapshotError,restore.RegisteredAssetRestoreError)):
        exercise(tmp_path,**{fault:True})


@pytest.mark.skipif(os.name=='nt',reason='Original POSIX stat hardlink guard')
def test_posix_original_nlink_rejection_is_retained(tmp_path):
    target=tmp_path/'target';target.mkdir(mode=0o700)
    policy=snapshot.Policy(100,64,1024,1024,1024,1024)
    with restore._OwnedGeneration(target,policy)as g:
        path=g.work/'one.txt';path.write_bytes(b'public synthetic')
        os.link(path,tmp_path/'alias')
        with pytest.raises(restore.RegisteredAssetRestoreError,match='^REGISTERED_RESTORE_NAMESPACE_MISMATCH$'):
            g.validate_namespace({'one.txt'})


@pytest.mark.parametrize('links',[1,2])
def test_actual_held_windows_adapter_uses_native_link_count_and_closes_chain(links):
    from pathlib import PureWindowsPath
    class Kernel:
        def __init__(self):self.handles={};self.closed=[]
        def drive_type(self,root):return 3
        def open(self,path,*,directory):
            handle=len(self.handles)+1;self.handles[handle]=(path,directory);return handle
        def info(self,handle):
            path,directory=self.handles[handle]
            return {'identity':(7,handle),'file_type':1,'attributes':0x10 if directory else 0,'links':1 if directory else links,'size':8}
        def final_path(self,handle):return '\\\\?\\'+self.handles[handle][0]
        def close(self,handle):self.closed.append(handle)
    kernel=Kernel();policy=snapshot.Policy(100,64,1024,1024,1024,1024)
    with patch.object(os,'name','nt'),patch.object(snapshot,'local_absolute',lambda *_:PureWindowsPath(r'C:\owned')),patch.object(snapshot,'_CheckedWin32',lambda:kernel):
        if links==1:
            with snapshot.HeldFile(r'C:\owned','one.txt',policy)as held:held.check()
        else:
            with pytest.raises(snapshot.AssetSnapshotError,match='^ASSET_FILE_BOUND_OR_ALIAS$'):
                with snapshot.HeldFile(r'C:\owned','one.txt',policy):pass
    assert kernel.closed==[3,2,1]
