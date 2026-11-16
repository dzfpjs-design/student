"""Packaged worker resolution uses only a complete installed sibling tree."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from home_study_agent.smtp_owned_process import OwnedSmtpProcess
from contextlib import nullcontext


def make_tree(tmp_path,monkeypatch):
    service=tmp_path/'resources'/'local-agent'/'home-study-local-agent.exe'
    service.parent.mkdir(parents=True);service.write_bytes(b'synthetic-service')
    root=service.parent/'smtp-worker';(root/'_internal').mkdir(parents=True)
    files={'hsc-smtp-worker.exe':b'synthetic-worker','_internal/python.dll':b'synthetic-dependency'}
    for name,data in files.items():(root/name).write_bytes(data)
    manifest={'version':1,'files':{name:{'size':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in files.items()}}
    (root/'worker-manifest.json').write_text(json.dumps(manifest),encoding='utf8')
    monkeypatch.setattr(sys,'frozen',True,raising=False);monkeypatch.setattr(sys,'executable',str(service))
    return service,root


def test_frozen_path_is_fixed_sibling_onedir_with_no_onefile_fallback(tmp_path,monkeypatch):
    service,root=make_tree(tmp_path,monkeypatch)
    owner=OwnedSmtpProcess(nullcontext,lambda:True,lambda *args:None)
    assert owner._worker_command()==[str(root/'hsc-smtp-worker.exe')]
    (root/'hsc-smtp-worker.exe').unlink()
    with pytest.raises(ValueError,match='SMTP_WORKER_PACKAGE_INVALID'):owner._worker_command()


@pytest.mark.parametrize('damage',['missing_dependency','changed_dependency','extra_file','missing_manifest','extra_empty_directory'])
def test_incomplete_or_changed_worker_tree_is_rejected(tmp_path,monkeypatch,damage):
    service,root=make_tree(tmp_path,monkeypatch)
    if damage=='missing_dependency':(root/'_internal/python.dll').unlink()
    if damage=='changed_dependency':(root/'_internal/python.dll').write_bytes(b'changed')
    if damage=='extra_file':(root/'_internal/extra.dll').write_bytes(b'extra')
    if damage=='extra_empty_directory':(root/'_internal/unlisted').mkdir()
    if damage=='missing_manifest':(root/'worker-manifest.json').unlink()
    owner=OwnedSmtpProcess(nullcontext,lambda:True,lambda *args:None)
    with pytest.raises(ValueError,match='SMTP_WORKER_PACKAGE_INVALID'):
        owner.submit(host='127.0.0.1',port=2525,username='',password='',sender='a@example.test',recipient='b@example.test',mime_bytes=b'x',timeout_seconds=1,use_ssl=False,starttls=False)
    assert not owner.started


def test_unreadable_directory_walk_is_rejected_before_spawn(tmp_path,monkeypatch):
    _,root=make_tree(tmp_path,monkeypatch)
    def inaccessible(*args,**kwargs):
        assert kwargs.get('onerror') is not None
        kwargs['onerror'](PermissionError('synthetic directory read failure'))
        return iter(())
    monkeypatch.setattr(os,'walk',inaccessible)
    owner=OwnedSmtpProcess(nullcontext,lambda:True,lambda *args:None)
    with pytest.raises(ValueError,match='SMTP_WORKER_PACKAGE_INVALID'):owner._worker_command()
    assert not owner.started


def test_symlinked_nested_directory_is_rejected(tmp_path,monkeypatch):
    _,root=make_tree(tmp_path,monkeypatch)
    outside=tmp_path/'outside';outside.mkdir()
    link=root/'_internal'/'linked'
    try:link.symlink_to(outside,target_is_directory=True)
    except OSError:pytest.skip('symbolic link creation unavailable')
    with pytest.raises(ValueError,match='SMTP_WORKER_PACKAGE_INVALID'):
        OwnedSmtpProcess(nullcontext,lambda:True,lambda *args:None)._worker_command()


def test_symlinked_ancestor_is_rejected(tmp_path,monkeypatch):
    service,root=make_tree(tmp_path,monkeypatch)
    link=tmp_path/'linked'
    try: link.symlink_to(service.parent,target_is_directory=True)
    except OSError: pytest.skip('symbolic link creation unavailable')
    monkeypatch.setattr(sys,'executable',str(link/service.name))
    with pytest.raises(ValueError,match='SMTP_WORKER_PACKAGE_INVALID'):
        OwnedSmtpProcess(nullcontext,lambda:True,lambda *args:None)._worker_command()


@pytest.mark.skipif(os.name!='nt',reason='Windows junction verification')
def test_windows_junction_ancestor_is_rejected(tmp_path,monkeypatch):
    service,root=make_tree(tmp_path,monkeypatch)
    link=tmp_path/'junction'
    result=subprocess.run(['cmd','/c','mklink','/J',str(link),str(service.parent)],capture_output=True,timeout=10)
    assert result.returncode==0
    monkeypatch.setattr(sys,'executable',str(link/service.name))
    with pytest.raises(ValueError,match='SMTP_WORKER_PACKAGE_INVALID'):
        OwnedSmtpProcess(nullcontext,lambda:True,lambda *args:None)._worker_command()


@pytest.mark.skipif(os.name!='nt',reason='Windows junction verification')
def test_windows_nested_junction_is_rejected(tmp_path,monkeypatch):
    _,root=make_tree(tmp_path,monkeypatch)
    outside=tmp_path/'outside';outside.mkdir()
    link=root/'_internal'/'linked'
    result=subprocess.run(['cmd','/c','mklink','/J',str(link),str(outside)],capture_output=True,timeout=10)
    assert result.returncode==0
    with pytest.raises(ValueError,match='SMTP_WORKER_PACKAGE_INVALID'):
        OwnedSmtpProcess(nullcontext,lambda:True,lambda *args:None)._worker_command()
