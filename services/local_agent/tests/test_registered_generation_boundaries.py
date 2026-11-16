"""Main generation admission, projection and target-capability controls.

All inputs are synthetic. The legacy no-witness stage below simulates a caller-
authenticated legacy contract; it is not an encrypted-capsule/AEAD test. Actual
AEAD integration lives in test_registered_generation_fullschema.py.
"""
from pathlib import Path,PureWindowsPath
import ctypes
import json
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from test_registered_generation_fullschema import registered_generation,sha,fp
from home_study_agent import registered_asset_snapshot as snapshot
from home_study_agent import registered_asset_restore as restore
from home_study_agent import registered_asset_backup as backup


def test_legacy_pdf_without_verified_witness_is_not_retroactively_trusted(registered_generation):
    f=registered_generation
    # Plain synthetic stage with the legacy version-list shape. No old provider
    # is loaded and no authentication result is invented for this admission test.
    policy=snapshot.Policy(1000,64,1024,1024*1024,64*1024*1024,32*1024*1024)
    stage=snapshot.create_registered_asset_snapshot(f.cold,f.owned,policy)
    manifest=json.loads((stage/'manifest.json').read_bytes())
    pdf=next(a for a in manifest['assets']if a['kind']=='kg'and a['role']=='pdf')
    witness=sha(snapshot.canonical(snapshot.registered_fingerprint_witness('pdf',fp(f.pdf),pdf['sha256'])))
    assert witness in pdf['version_sha256s'] and len(pdf['version_sha256s'])==2
    pdf['version_sha256s'].remove(witness)
    raw=snapshot.canonical(manifest);(stage/'manifest.json').write_bytes(raw)
    assert snapshot.verify_registered_asset_snapshot(stage,policy)['manifest_sha256']==sha(raw)
    # The public component takes an already-authenticated summary from its
    # caller. These explicit placeholders grant shape only, never AEAD credit.
    contract={'coverage':'encrypted_registered_v1','asset_count':len(manifest['assets']),
              'total_bytes':sum(a['bytes']for a in manifest['assets']),
              'manifest_sha256':sha(raw),'coverage_sha256':sha(snapshot.canonical(manifest['coverage'])),
              'capsule':{'size_bytes':1,'sha256':'0'*64}}
    cold=f.cold.read_bytes()
    with pytest.raises(restore.RegisteredAssetRestoreError,match='^REGISTERED_RESTORE_FINGERPRINT_WITNESS_REQUIRED$'):
        f.prepare(stage,contract,policy)
    assert f.cold.read_bytes()==cold
    f.sentinels()


def test_verified_witness_requires_same_held_source_through_copy(registered_generation):
    f=registered_generation;original=snapshot._Collector.add;changed=False
    def mutate(collector,kind,key,role,root,relative,**kwargs):
        nonlocal changed
        result=original(collector,kind,key,role,root,relative,**kwargs)
        if role=='pdf'and not changed:
            changed=True;s=f.pdf.stat();os.utime(f.pdf,ns=(s.st_atime_ns,s.st_mtime_ns+1000))
        return result
    with patch.object(snapshot._Collector,'add',mutate):
        with pytest.raises(snapshot.AssetSnapshotError,match='^ASSET_CHANGED$'):f.capsule()
    assert changed and not backup.capsule_path(f.cold).exists();f.sentinels()


@pytest.mark.skipif(os.name=='nt',reason='POSIX timestamp-rounding fault injection; Windows uses SetFileTime')
def test_timestamp_precision_mismatch_is_not_consumer_success(registered_generation):
    f=registered_generation;stage,contract,policy=f.capsule();original=os.utime
    def rounded(path,*args,**kwargs):
        ns=kwargs['ns'];kwargs['ns']=(ns[0],ns[1]-1);return original(path,*args,**kwargs)
    with patch.object(restore.os,'utime',rounded):
        with pytest.raises(restore.RegisteredAssetRestoreError,match='^REGISTERED_RESTORE_TIMESTAMP_PRECISION$'):f.prepare(stage,contract,policy)
    f.sentinels()


@pytest.mark.parametrize('body',["UPDATE kg_corpus SET transcript_root='forged'WHERE corpus_id=NEW.corpus_id;",'SELECT 1;','DELETE FROM kg_resource;'])
def test_trigger_context_is_denied_even_for_same_allowed_column_or_read(registered_generation,body):
    f=registered_generation
    # Valid ordinary SQLite trigger bodies, not malformed SQL fixtures.
    with f.db.connect()as c:
        c.execute('CREATE TRIGGER boundary BEFORE UPDATE OF transcript_root ON kg_corpus BEGIN '+body+' END');c.commit()
    stage,contract,policy=f.capsule()
    with pytest.raises(restore.RegisteredAssetRestoreError):f.prepare(stage,contract,policy)
    f.sentinels()


def test_nested_manifest_bound_checked_before_chunk_materialization(tmp_path):
    db=tmp_path/'closed.db'
    with sqlite3.connect(db)as c:
        c.execute('CREATE TABLE curriculum_standard_source(source_id,source_key,local_root_path,pdf_filename,pages_filename,pdf_sha256,text_sha256)')
        c.execute("INSERT INTO curriculum_standard_source VALUES ('s','s','/original-root',NULL,NULL,NULL,NULL)")
    policy=snapshot.Policy(100,64,1024,8,100000,100000)
    name='registered/standard/'+snapshot.record_key('standard','s')+'/manifest/manifest.json'
    item={'logical_name':name,'kind':'standard','record_sha256':snapshot.record_key('standard','s'),'role':'manifest','relative_path':'manifest.json','storage':'regular','sha256':'0'*64,'bytes':9,'version_sha256s':[sha(snapshot.canonical(None))]}
    plan=restore._Plan(db,tmp_path/'payloads',{'assets':[item],'coverage':[]},policy,tmp_path/'final')
    reads=[]
    class Held:
        def __init__(self,*a):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def verified_length(self):return 9
        def chunks(self):reads.append(True);yield b'x'*9
    with patch.object(restore,'HeldFile',Held):
        with pytest.raises(restore.RegisteredAssetRestoreError,match='^REGISTERED_RESTORE_MANIFEST_BOUND$'):plan.build()
    assert reads==[]


class FakeKernel:
    def __init__(self,fault=None):self.events=[];self.fault=fault
    def info(self,h):
        return {'file_type':1,'attributes':0x400 if self.fault=='reparse'and h==2 else 0x10,'identity':(7,8,9)if h==1 or self.fault!='identity'else(0,0,0)}
    def final_path(self,h):return r'\\?\C:\owned'if self.fault!='path'or h==1 else r'\\?\C:\outside'
    def open_target(self,path,**kw):self.events.append(('open',path,kw));return 2
    def flush(self,h):
        self.events.append(('flush',h))
        if self.fault=='flush':raise OSError(5,'synthetic')
    def close(self,h):self.events.append(('close',h))


@pytest.mark.parametrize('fault',[None,'identity','path','reparse','flush'])
def test_target_temporary_write_flush_lifecycle(fault):
    g=restore._OwnedGeneration.__new__(restore._OwnedGeneration)
    g.kernel=FakeKernel(fault);g.target=PureWindowsPath(r'C:\owned');g.work=g.target/'work'
    g.handles=[(1,str(g.target),(7,8,9))];g.dirs={};g.check=lambda:g.kernel.events.append(('chain_check',))
    with patch.object(restore.os,'name','nt'):
        if fault is None:g.flush_directory(1)
        else:
            with pytest.raises(restore.RegisteredAssetRestoreError):g.flush_directory(1)
    assert [e for e in g.kernel.events if e[0]=='open']==[('open',r'C:\owned',{'write':True})]
    assert [e for e in g.kernel.events if e[0]=='close']==[('close',2)]
    if fault is None:
        assert ('flush',2)in g.kernel.events
        assert g.kernel.events[-1]==('chain_check',)
    elif fault in{'identity','path','reparse'}:assert ('flush',2)not in g.kernel.events


def test_target_access_shares_have_no_delete_and_no_resident_write():
    calls=[]
    class DLL:
        def CreateFileW(self,*args):calls.append(args);return 123
    kernel=restore._TargetWin32.__new__(restore._TargetWin32);kernel.ct=ctypes;kernel.dll=DLL()
    kernel.open_target(r'C:\owned');kernel.open_target(r'C:\owned',write=True);kernel.open_target(r'C:\owned\asset',file_attributes=True)
    assert [a[1]for a in calls]==[0x80,0x40000080,0x40000180]
    assert all(a[2]==3 and not a[2]&4 for a in calls)
    assert all(a[5]&0x00200000 for a in calls)


def test_source_holds_are_closed_at_every_flush(registered_generation):
    f=registered_generation;stage,contract,policy=f.capsule();active=0
    enter=restore.HeldFile.__enter__;leave=restore.HeldFile.__exit__;flush=restore._OwnedGeneration.flush_directory
    def start(held):
        nonlocal active
        result=enter(held);active+=1;return result
    def end(held,*args):
        nonlocal active
        try:return leave(held,*args)
        finally:active-=1
    def checked(g,h):
        assert active==0
        return flush(g,h)
    with patch.object(restore.HeldFile,'__enter__',start),patch.object(restore.HeldFile,'__exit__',end),patch.object(restore._OwnedGeneration,'flush_directory',checked):
        result=f.prepare(stage,contract,policy)
    assert active==0 and result.database_path.exists();f.sentinels()


def test_fresh_pdf_missing_original_fingerprint_never_fabricates_witness(registered_generation):
    f=registered_generation
    with sqlite3.connect(f.meta)as c:c.execute('ALTER TABLE textbook_book DROP COLUMN fingerprint')
    with pytest.raises(snapshot.AssetSnapshotError,match='^REGISTERED_PDF_FINGERPRINT_REQUIRED$'):f.capsule()
    assert not backup.capsule_path(f.cold).exists();f.sentinels()


def test_postwrite_projection_guard_detects_nonroot_change():
    with sqlite3.connect(':memory:')as c:
        c.executescript("CREATE TABLE kg_corpus(corpus_id,transcript_root);INSERT INTO kg_corpus VALUES ('one','old');CREATE TABLE kg_resource(resource_id,transcript_relative_path);INSERT INTO kg_resource VALUES ('r','one.txt');")
        rewrites=[('kg_corpus','corpus_id','one','transcript_root','new')]
        expected=restore._projection_digest(c,rewrites)
        c.execute("UPDATE kg_corpus SET transcript_root='new'")
        restore._verify_rewritten_projection(c,expected,rewrites)
        c.execute("UPDATE kg_resource SET transcript_relative_path='missing.txt'")
        with pytest.raises(restore.RegisteredAssetRestoreError,match='^REGISTERED_RESTORE_PROJECTION_CHANGED$'):
            restore._verify_rewritten_projection(c,expected,rewrites)


def test_prepared_database_file_flush_has_temporary_write_and_close():
    g=restore._OwnedGeneration.__new__(restore._OwnedGeneration);g.work=PureWindowsPath(r'C:\owned\work')
    class FileKernel(FakeKernel):
        def info(self,h):return {'file_type':1,'attributes':0,'identity':(7,8,9),'links':1}
        def final_path(self,h):return r'\\?\C:\owned\work\database.db'
    g.kernel=FileKernel();g.check=lambda:None;g.mkdir=lambda _:1;g.release_dirs=lambda:None;g.policy=snapshot.Policy(100,64,1024,1024,1024,1024)
    with patch.object(restore.os,'name','nt'):g.flush_file('database.db')
    assert g.kernel.events==[('open',r'C:\owned\work\database.db',{'file_attributes':True}),('flush',2),('close',2)]
