"""Internal scratch canonicalization never weakens external path admission."""
import os
import sqlite3
from pathlib import Path
import pytest
from home_study_agent.database import Database
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent import privacy_lifecycle as lifecycle
from home_study_agent.restore_publication import _Parent, RestorePublicationError

@pytest.mark.skipif(os.name == "nt", reason="POSIX system temporary-root symlink regression")
def test_internal_alias_scratch_canonicalized_before_original_initialize(tmp_path,monkeypatch):
    physical=tmp_path/'scratch';physical.mkdir();alias=tmp_path/'stdlib-temp-alias';alias.symlink_to(physical,target_is_directory=True)
    source=tmp_path/'cold.db'
    with sqlite3.connect(source)as c:c.execute('CREATE TABLE synthetic(value)');c.execute('INSERT INTO synthetic VALUES(1)')
    source_raw=source.read_bytes();live=tmp_path/'live.db';live.write_bytes(b'original live sentinel')
    data=tmp_path/'formal-data';data.mkdir();seen=[]
    service=PrivacyLifecycleService(Database(live),data)
    monkeypatch.setattr(lifecycle.tempfile,'gettempdir',lambda:str(alias))
    class StopAfterOriginalGate(Exception):pass
    def admitted(db):
        seen.append(db.path)
        assert db.path.parent.parent==physical and db.path.parent.name.startswith('hsc-restore-staging-')
        assert not any(p.is_symlink()for p in [db.path.parent,*db.path.parent.parents])
        assert db.path.parent!=data and data not in db.path.parents
        raise StopAfterOriginalGate()
    monkeypatch.setattr(Database,'_initialize_admitted',admitted)
    with pytest.raises(StopAfterOriginalGate):service._restore_database_snapshot(source)
    assert len(seen)==1 and not seen[0].parent.exists()
    assert live.read_bytes()==b'original live sentinel'and source.read_bytes()==source_raw and list(data.iterdir())==[]

@pytest.mark.skipif(os.name == "nt", reason="POSIX system temporary-root symlink regression")
def test_untrusted_external_alias_still_refused_by_original_parent(tmp_path):
    physical=tmp_path/'data';physical.mkdir();alias=tmp_path/'external';alias.symlink_to(physical,target_is_directory=True)
    with pytest.raises((NotADirectoryError,RestorePublicationError,OSError)):
        with _Parent(alias):pass
    assert list(physical.iterdir())==[]

def test_missing_internal_scratch_root_fails_without_fallback(tmp_path,monkeypatch):
    source=tmp_path/'cold.db'
    with sqlite3.connect(source)as c:c.execute('CREATE TABLE synthetic(value)')
    data=tmp_path/'formal-data';data.mkdir();service=PrivacyLifecycleService(Database(data/'live.db'),data)
    monkeypatch.setattr(lifecycle.tempfile,'gettempdir',lambda:str(tmp_path/'missing-root'))
    with pytest.raises(FileNotFoundError):service._restore_database_snapshot(source)
    assert list(data.iterdir())==[]
