"""Public synthetic provenance, never family data or native-device credit."""
import base64
import asyncio
import hashlib
from copy import deepcopy
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.knowledge_graph.paper_objective_bridge import PaperObjectiveBridge, assert_source_current
from home_study_agent.knowledge_graph.parent_textbook_capture import ParentTextbookCaptureService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.standard_relocation_provenance import (
    NAME, StandardRelocationError, canonical, decode, digest, encode, revalidate,
)
from home_study_agent.local_supply import LocalSupplyError
from home_study_agent import original_connection_read as reads, restore_publication as pub
from test_paper_objective_bridge import fixture, command, CHILD
from test_companion_activity_host import NoCloud
from test_parent_textbook_capture import Adapter
from privacy_restore_operation import restore_operation

PASSPHRASE='public standard relocation synthetic passphrase'
OTHER='child-junior-3'


def _settings(root,key):
    return Settings(data_dir=root,database_path=root/'agent.db',data_encryption_key=key,
        native_launch_attestation_key=None,maintenance_enabled=False,agent_runtime_enabled=False,
        session_token='public-standard-relocation')


def _quiesce_original_visual(client):
    async def finish():
        tasks=[task for task in asyncio.all_tasks()if task.get_coro().__qualname__=='VisualCaptureProducer.maintain']
        assert len(tasks)==1
        tasks[0].cancel()
        try:await tasks[0]
        except asyncio.CancelledError:pass
    client.portal.call(finish)
    # Wait for the original SQL reader's actual finally; cancellation never
    # clears a gate or pretends a running thread has closed.
    with pub.publication_owner(client.app.state.privacy_lifecycle.database,timeout=5):pass


def _restore(root,archive,key,*,quiesce_visual=False):
    root.mkdir(mode=0o700)
    settings=_settings(root,key)
    with TestClient(create_app(settings,model_gateway=NoCloud()),headers={'X-HSC-Session':settings.session_token})as client:
        if quiesce_visual:_quiesce_original_visual(client)
        setup=client.post('/v1/parent/setup',json={'pin':'4826'});assert setup.status_code==200,setup.text
        headers={'X-HSC-Parent':setup.json()['parent_token']}
        operation=restore_operation(client,headers=headers)
        result=client.post('/v1/parent/backups/portable/restore',headers=headers,json={
            'path':str(archive),'passphrase':PASSPHRASE,'confirmation':'确认迁移恢复','operation':operation})
        assert result.status_code==200,result.text
        assert result.json()['status']=='restored'
    return Database(settings.database_path,media_root=root,data_encryption_key=key)


def test_closed_data_encoding_never_normalizes_old_roots_or_accepts_extra_fields():
    source={'source_id':'std','local_root_path':'/public/数学','title':'公开课标'}
    entry={'source_id':'std','old_root':source['local_root_path'],'old_source_sha256':digest(source)}
    raw=encode([entry]);assert decode(raw)==[entry]
    assert b'\\u6570'not in raw and json.loads(raw)['entries'][0]['old_root']=='/public/数学'
    assert revalidate([entry],{'std':source})==[entry]
    for bad in [raw+b' ',raw.replace(b'"format":',b'"format":"duplicate","format":'),
                canonical({'format':'hsc_standard_relocation_v1','entries':[{**entry,'flag':True}]}),
                canonical({'format':'hsc_standard_relocation_v1','entries':[entry,entry]})]:
        with pytest.raises((ValueError,StandardRelocationError)):decode(bad)
    with pytest.raises(ValueError):encode([{**entry,'old_root':'/public/../foreign'}])
    with pytest.raises(ValueError):revalidate([entry],{'std':{**source,'title':'changed'}})


def test_original_connection_locators_are_not_read_capabilities_or_transaction_owners(tmp_path):
    db=Database(tmp_path/'original.db');db.initialize()
    with db.connect()as original:
        locator=original._original_read_locator
        reads._state(original,None)
        with db.connect()as foreign:
            foreign._original_read_locator=locator
            with pytest.raises(pub.RestorePublicationError):reads._state(foreign,None)
        raw=sqlite3.connect(db.path)
        try:
            with pytest.raises(pub.RestorePublicationError):reads._state(raw,None)
        finally:raw.close()
        original.execute('BEGIN')
        with pytest.raises(pub.RestorePublicationError):
            with reads._verification(original,None):pass
        # A real caller cannot borrow a with-body proof, even around a no-write
        # rollback/re-BEGIN. The private scope never takes over this transaction.
        original.rollback();original.execute('BEGIN')
        with pytest.raises(pub.RestorePublicationError):
            with reads._verification(original,None):pass
        assert original.in_transaction
        original.rollback()
    with pytest.raises(pub.RestorePublicationError):reads._state(original,None)
    assert locator not in reads._CONNECTIONS and not db._original_connections


def test_business_read_preserves_original_borrowed_uncommitted_write(tmp_path):
    root=tmp_path/'borrowed';root.mkdir(mode=0o700)
    db,_,_,_=fixture(root)
    bridge=PaperObjectiveBridge(db);sent=bridge.submit(CHILD,**command(bridge.options(CHILD)))
    with db.connect()as c:
        c.execute('BEGIN IMMEDIATE')
        nickname=c.execute('SELECT nickname FROM child_profile WHERE child_id=?',(CHILD,)).fetchone()[0]
        c.execute('UPDATE child_profile SET nickname=? WHERE child_id=?',('public uncommitted',CHILD))
        source=dict(c.execute('SELECT * FROM knowledge_source WHERE source_id=?',(sent['source_id'],)).fetchone())
        assert assert_source_current(c,None,CHILD,source)
        assert c.in_transaction and c.execute('SELECT nickname FROM child_profile WHERE child_id=?',(CHILD,)).fetchone()[0]=='public uncommitted'
        c.rollback()
        assert c.execute('SELECT nickname FROM child_profile WHERE child_id=?',(CHILD,)).fetchone()[0]==nickname


@pytest.mark.parametrize('end_transaction',['rollback','commit'])
def test_original_export_rejects_no_dml_transaction_replacement_before_backup_success(tmp_path,monkeypatch,end_transaction):
    from home_study_agent import registered_asset_backup as registered
    from home_study_agent import standard_relocation_provenance as provenance
    from home_study_agent.privacy_lifecycle import InvalidBackupError
    root=tmp_path/'replacement';root.mkdir(mode=0o700)
    db=Database(root/'agent.db',media_root=root,data_encryption_key=b'0'*32);db.initialize()
    original=registered.create_capsule;seen={}
    def replace_transaction(snapshot,key,encrypt_file,**kwargs):
        ticket=kwargs['provenance_ticket'];state=provenance._EXPORTS[ticket]
        c=state['connection']();before=c.total_changes
        getattr(sqlite3.Connection,end_transaction)(c)
        sqlite3.Connection.execute(c,'BEGIN')
        assert c.total_changes==before and c.in_transaction
        seen.update(ticket=ticket,snapshot=snapshot,before=before)
        return original(snapshot,key,encrypt_file,**kwargs)
    monkeypatch.setattr(registered,'create_capsule',replace_transaction)
    with pytest.raises(InvalidBackupError,match='REGISTERED_ASSET_BACKUP_FAILED')as failure:
        PrivacyLifecycleService(db,root,b'0'*32).create_backup(prune=False)
    assert isinstance(failure.value.__cause__,pub.RestorePublicationError)
    assert str(failure.value.__cause__)=='RESTORE_PUBLICATION_SNAPSHOT_UNCONFIRMED'
    assert seen['ticket']not in provenance._EXPORTS
    with pytest.raises(StandardRelocationError):
        provenance.authorize_export(seen['ticket'],seen['snapshot'],'foreign',str(root))
    # Inspection uses a separate synthetic read-only SQL connection; it never
    # clears the poisoned original admission or grants it another capability.
    with sqlite3.connect(db.path.as_uri()+'?mode=ro',uri=True)as inspect:
        assert inspect.execute('SELECT COUNT(*) FROM backup_manifest').fetchone()[0]==0
        assert inspect.execute("SELECT COUNT(*) FROM audit_event WHERE action='backup_created'").fetchone()[0]==0
    assert not list((root/'backups').iterdir())
    gate=pub._gate(pub._path(db.path))
    assert gate.poisoned and gate.retained and not db._original_connections


@pytest.mark.parametrize('unrelated_members',[120,240])
@pytest.mark.skipif(os.name!='posix',reason='POSIX descriptor cost boundary; original Windows handles need target evidence')
def test_two_real_standard_roots_export_with_sequential_unrelated_original_handles(tmp_path,monkeypatch,record_property,unrelated_members):
    from home_study_agent.knowledge_graph.parent_textbook_drafts import ParentTextbookDraftService
    from home_study_agent.knowledge_ingestion.document_evidence import DocumentCorpusSpec,LocalDocumentEvidenceIngestor
    from home_study_agent.registered_asset_snapshot import HeldFile
    source=tmp_path/'source';source.mkdir(mode=0o700)
    db,drafts,saved,value=fixture(source);bridge=PaperObjectiveBridge(db)
    first=bridge.submit(CHILD,**command(bridge.options(CHILD)))
    second_root=source/'second-standard';second_root.mkdir()
    (second_root/'manifest.json').write_text(json.dumps({'format':'hsc_official_curriculum_sources_v1',
        'sources':[{'source_key':'public-second-standard','pdf_filename':None,'pages_filename':None}]}),encoding='utf-8')
    with db.connect()as c:
        # The public fixture copies only the existing business row shape. No
        # restore/control/authority row is manufactured by the fixture.
        for table,where,changes in [
            ('curriculum_standard_source',"source_id='std'",{'source_id':'std-two','source_key':'public-second-standard','local_root_path':str(second_root),'title':'公开第二课标'}),
            ('curriculum_standard_page',"source_id='std'",{'source_id':'std-two'}),
            ('curriculum_standard_requirement',"requirement_id='req'",{'source_id':'std-two','requirement_id':'req-two'})]:
            row=dict(c.execute('SELECT * FROM '+table+' WHERE '+where).fetchone());row.update(changes)
            c.execute('INSERT INTO '+table+'('+','.join(row)+') VALUES('+','.join('?'for _ in row)+')',tuple(row.values()))
        c.commit()
    next_value=deepcopy(value);next_value['objective_draft'].update(question_number='7',full_question='公开合成另一原题：四加五等于多少？',official_answers=['9'])
    drafts.save(CHILD,'capture',saved['revision'],'public-second-standard-draft',next_value,expected_generation=saved['storage_generation'])
    options=bridge.options(CHILD);body=command(options);chosen=next(v for v in options['standards']if v['requirement_id']=='req-two')
    body.update(standard_key=chosen['key'],standard_binding=chosen['binding'],action_id='public-second-standard-submit')
    second=bridge.submit(CHILD,**body)
    text_root=source/'other-public-transcripts';text_root.mkdir()
    for index in range(unrelated_members):
        (text_root/f'page-{index:04d}.txt').write_text(f'Public synthetic unrelated page {index}: local proof remains bounded.',encoding='utf-8')
    spec=DocumentCorpusSpec('public-unrelated-pages','公开无关页','other_user_document',text_root,None,
        'public-synthetic-local','family_owned_local','synthetic')
    ingestor=LocalDocumentEvidenceIngestor(db);job=ingestor.start(spec,idempotency_key='public-bounded-handles')
    ingestor.run(spec,job,max_resources=1,throttle_ms=0)
    archive=PrivacyLifecycleService(db,source,b'0'*32).create_portable_backup(PASSPHRASE)['path']
    restored=_restore(tmp_path/'restored',archive,b'1'*32)
    # An export consumes two distinct held target sets in the same generation.
    again=PrivacyLifecycleService(restored,restored.path.parent,b'1'*32).create_portable_backup(PASSPHRASE)
    assert Path(again['path']).is_file()
    with restored.connect()as c:
        roots={r['source_id']:Path(r['local_root_path'])for r in c.execute('SELECT source_id,local_root_path FROM curriculum_standard_source')}
        count=c.execute("SELECT COUNT(*) FROM restore_asset_member WHERE kind='file' AND state='READY'").fetchone()[0]
        assert count>=unrelated_members+2
    original_enter=HeldFile.__enter__;original_exit=HeldFile.__exit__;original_hash=HeldFile.hash
    active=set();peak={'held':0,'process':0};baseline=len(os.listdir('/dev/fd'));current={'root':None}
    def entered(held):
        result=original_enter(held);active.add(id(held))
        peak['held']=max(peak['held'],len(active));peak['process']=max(peak['process'],len(os.listdir('/dev/fd')))
        return result
    def exited(held,*args):
        result=original_exit(held,*args);active.remove(id(held));return result
    def hashed(held):
        assert held.root.joinpath(*held.parts).is_relative_to(current['root']),'unrelated member bytes must not be hashed by standard provenance'
        return original_hash(held)
    start=time.monotonic()
    with monkeypatch.context()as m:
        m.setattr(HeldFile,'__enter__',entered);m.setattr(HeldFile,'__exit__',exited);m.setattr(HeldFile,'hash',hashed)
        for receipt,source_id in [(first,'std'),(second,'std-two')]:
            current['root']=roots[source_id]
            assert PaperObjectiveBridge(restored).read(CHILD,receipt['receipt_id'])==receipt
    assert not active and peak['held']<=8 and peak['process']-baseline<=128
    record_property('unrelated_ready_members',unrelated_members);record_property('peak_original_held_files',peak['held'])
    record_property('process_fd_delta_sampled_at_each_original_file_enter',peak['process']-baseline);record_property('two_roots_read_seconds',time.monotonic()-start)


def test_new_same_root_source_exports_without_inheriting_legacy_authority_or_forgetting_missing_enrolled_rows(tmp_path):
    from home_study_agent.privacy_lifecycle import InvalidBackupError
    source=tmp_path/'source';source.mkdir(mode=0o700)
    db,_,_,_=fixture(source);bridge=PaperObjectiveBridge(db)
    sent=bridge.submit(CHILD,**command(bridge.options(CHILD)))
    manifest=source/'standards/manifest.json';value=json.loads(manifest.read_bytes())
    value['sources'].append({'source_key':'public-later-source','pdf_filename':None,'pages_filename':None})
    manifest.write_text(json.dumps(value),encoding='utf-8')
    archive=PrivacyLifecycleService(db,source,b'0'*32).create_portable_backup(PASSPHRASE)['path']
    one=_restore(tmp_path/'one',archive,b'1'*32)
    with one.connect()as c:
        row=dict(c.execute("SELECT * FROM curriculum_standard_source WHERE source_id='std'").fetchone())
        row.update(source_id='later-source',source_key='public-later-source',title='公开后来独立来源')
        c.execute('INSERT INTO curriculum_standard_source('+','.join(row)+') VALUES('+','.join('?'for _ in row)+')',tuple(row.values()));c.commit()
    archive2=PrivacyLifecycleService(one,one.path.parent,b'1'*32).create_portable_backup(PASSPHRASE)['path']
    two=_restore(tmp_path/'two',archive2,b'2'*32)
    assert PaperObjectiveBridge(two).read(CHILD,sent['receipt_id'])==sent
    with two.connect()as c:
        paths={r['source_id']:r['local_root_path']for r in c.execute('SELECT source_id,local_root_path FROM curriculum_standard_source')}
        assert paths['std']==paths['later-source']
        entries=decode((Path(paths['std'])/NAME).read_bytes())
        assert {entry['source_id']for entry in entries}=={'std'}
        metadata=c.execute("SELECT reference_json FROM restore_asset_member WHERE purpose='metadata' AND relative_name LIKE ?",('%/'+NAME,)).fetchone()
        assert {ref['id']for ref in json.loads(metadata[0])}=={'std'}
        # A plain new source row cannot receive the old source's authority.
        c.execute('BEGIN');c.execute("UPDATE curriculum_standard_requirement SET source_id='later-source' WHERE requirement_id='req'")
        current=dict(c.execute('SELECT * FROM knowledge_source WHERE source_id=?',(sent['source_id'],)).fetchone())
        with pytest.raises((ValueError,pub.RestorePublicationError)):assert_source_current(c,None,CHILD,current)
        c.rollback()
    assert PaperObjectiveBridge(two).read(CHILD,sent['receipt_id'])==sent
    with two.connect()as c:
        before=c.execute('SELECT COUNT(*) FROM backup_manifest').fetchone()[0]
        # This is deliberate SQL tampering, not a legitimate erase. Even with
        # no FK orphan, absence of the old row must not prune its enrolled fact.
        c.execute("DELETE FROM curriculum_standard_requirement WHERE source_id='std'")
        c.execute("DELETE FROM curriculum_standard_page WHERE source_id='std'")
        c.execute("DELETE FROM curriculum_standard_source WHERE source_id='std'");c.commit()
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
    with pytest.raises(InvalidBackupError):PrivacyLifecycleService(two,two.path.parent,b'2'*32).create_backup(prune=False)
    with two.connect()as c:assert c.execute('SELECT COUNT(*) FROM backup_manifest').fetchone()[0]==before


@pytest.mark.parametrize('lane',['native_child_media','parent_capture_raw'])
def test_other_child_original_native_media_erasure_keeps_this_child_frozen_standard_readable(tmp_path,monkeypatch,record_property,lane):
    from home_study_agent.parent_auth import ParentAuth
    from test_native_privacy_permit_http import certificate,signed_header
    from test_controlled_launch_intent_http import KEY
    source=tmp_path/'source';source.mkdir(mode=0o700)
    db,_,_,_=fixture(source);ParentAuth(db).setup('4826')
    bridge=PaperObjectiveBridge(db);sent=bridge.submit(CHILD,**command(bridge.options(CHILD)))
    image=io.BytesIO();Image.new('RGB',(32,32),color=(20,90,140)).save(image,format='PNG')
    artifact=ParentTextbookCaptureService(db,Adapter()).recognize_file(OTHER,
        base64.b64encode(image.getvalue()).decode(),{'title':'公开另一孩原页','page_number':1},'public-erasure-other-child')
    other_id=ParentTextbookCaptureService(db,None)._id_for_ref(artifact['capture_ref'])
    frame='public-parent-only-original-frame'
    if lane=='parent_capture_raw':
        from home_study_agent.document_trial_capture import FRAME_TYPE
        settings=replace(_settings(source,b'0'*32),database_path=db.path)
        with TestClient(create_app(settings,model_gateway=NoCloud()),headers={'X-HSC-Session':settings.session_token})as client:
            session=client.post('/v1/sessions',json={'child_id':OTHER,'client_action_id':'public-original-frame-session'})
            assert session.status_code==201,session.text
            sid=session.json()['session_id'];actual=client.app.state.privacy_lifecycle.database
            path=client.app.state.media_ai._store_raw(OTHER,sid,frame,'.png',image.getvalue())
            with actual.connect()as c:
                c.execute("INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,local_path,extracted_text,confidence,details_json,status,created_at,client_action_id)VALUES(?,?,?,?,?,'public synthetic',1,?,'completed',datetime('now'),?)",
                    (frame,OTHER,sid,FRAME_TYPE,path,json.dumps({'sha256':hashlib.sha256(image.getvalue()).hexdigest(),'observed_at':'2026-10-21T00:00:00+00:00','reason':'synthetic-original-frame','width':32,'height':32}),frame+'-action'))
                c.execute("UPDATE daily_session SET status='completed',state='completed'WHERE session_id=?",(sid,));c.commit()
            assert client.app.state.document_trial_capture.image(OTHER,frame)==image.getvalue()
    archive=PrivacyLifecycleService(db,source,b'0'*32).create_portable_backup(PASSPHRASE)['path']
    restored=_restore(tmp_path/'restored',archive,b'1'*32,quiesce_visual=lane=='native_child_media')
    assert PaperObjectiveBridge(restored).read(CHILD,sent['receipt_id'])==sent
    settings=replace(_settings(restored.path.parent,b'1'*32),native_launch_attestation_key=KEY)
    original_rotate=PrivacyLifecycleService._rotate_recovery_points_after_deletion;pending_reads=[]
    def current_erase_rotation(service,*args,**kwargs):
        # A current erase does not authorize ordinary eligibility to borrow a
        # pending preimage. Only the exact original export below may do that.
        with pytest.raises((ValueError,pub.RestorePublicationError)):
            PaperObjectiveBridge(service.database).read(CHILD,sent['receipt_id'])
        pending_reads.append('ordinary_read_rejected')
        return original_rotate(service,*args,**kwargs)
    monkeypatch.setattr(PrivacyLifecycleService,'_rotate_recovery_points_after_deletion',current_erase_rotation)
    with TestClient(create_app(settings,model_gateway=NoCloud()),headers={'X-HSC-Session':settings.session_token})as client:
        if lane=='native_child_media':_quiesce_original_visual(client)
        unlock=client.post('/v1/parent/unlock',json={'pin':'4826'});assert unlock.status_code==200,unlock.text
        headers={'X-HSC-Parent':unlock.json()['parent_token']}
        if lane=='native_child_media':
            operation=restore_operation(client,headers=headers);action=operation['request_id']
            path=f'/v1/parent/children/{OTHER}/delete'
            body={'scope':'media','confirmation':'确认删除','client_action_id':action,'operation':operation}
            proof=certificate(path,body,operation,child_id=OTHER,scope='media')
            result=client.post(path,headers={**headers,**signed_header(proof)},json=body)
        else:
            action='public-parent-erase-original-frame'
            before=client.app.state.document_trial_capture.image(OTHER,frame);assert before==image.getvalue()
            result=client.request('DELETE',f'/v1/parent/capture-artifacts/{frame}/raw-media',headers=headers,
                json={'confirmation':'确认删除','client_action_id':action})
        assert result.status_code==200,result.text
        assert result.json()['status']=='completed'
        deletion=result.json()['deletion_id'];replacement_id=result.json()['recovery_rotation']['replacement_backup_id']
    assert pending_reads==['ordinary_read_rejected']
    if lane=='native_child_media':
        with pytest.raises(ValueError):ParentTextbookCaptureService(restored,None)._read_raw_page(OTHER,other_id)
    assert PaperObjectiveBridge(restored).read(CHILD,sent['receipt_id'])==sent
    with restored.connect()as c:
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        table='restore_asset_erasure'if lane=='native_child_media'else'restore_asset_parent_erasure'
        assert c.execute("SELECT COUNT(*) FROM "+table+" WHERE state='ERASED'").fetchone()[0]>0
        live_job=dict(c.execute('SELECT *FROM deletion_job WHERE deletion_id=?',(deletion,)).fetchone())
        intent=c.execute('SELECT *FROM privacy_operation_intent WHERE request_id=?',(action,)).fetchone()
        live_intent=dict(intent)if intent is not None else None
        if live_intent is not None:assert live_intent['state']=='finished'and live_intent['reference_id']==deletion
    # The rotation target was captured before the original erase permit/tail
    # finished. It must remain an authentic usable backup after completion,
    # without importing that source's pending operation as live authority.
    from synthetic_native_restore_request import signed_restore_post
    historical_checks=set()
    if lane=='native_child_media':
        original_committed=pub._committed
        def checked_history(c,j,owner=None,*,historical=False):
            if historical and j['schema']==3:
                # The actual previous Parent result cannot self-authorize a
                # current commit or accept a forged marker/phase/owner.
                for label,changed,changed_owner in(
                    ('phase',{**j,'phase':'PREPARED'},owner),
                    ('marker',{**j,'binding_sha256':'0'*64},owner),
                    ('owner',j,None)):
                    with pytest.raises(pub.RestorePublicationError):
                        original_committed(c,changed,changed_owner,historical=True)
                    historical_checks.add(label)
                with pytest.raises(pub.RestorePublicationError):
                    original_committed(c,j,owner,historical=False)
                historical_checks.add('current_parent_policy_still_strict')
            return original_committed(c,j,owner,historical=historical)
        monkeypatch.setattr(pub,'_committed',checked_history)
    with TestClient(create_app(settings,model_gateway=NoCloud()),headers={'X-HSC-Session':settings.session_token})as client:
        if lane=='native_child_media':_quiesce_original_visual(client)
        unlock=client.post('/v1/parent/unlock',json={'pin':'4826'});assert unlock.status_code==200,unlock.text
        headers={'X-HSC-Parent':unlock.json()['parent_token']}
        listed=client.get('/v1/parent/backups',headers=headers);assert listed.status_code==200,listed.text
        replacement=next(row for row in listed.json()['items']if row['backup_id']==replacement_id)
        assert replacement['reason']=='post_deletion'
        actual_backup=PrivacyLifecycleService(client.app.state.privacy_lifecycle.database,restored.path.parent,b'1'*32)
        assert actual_backup._backup_checksum(Path(replacement['path']))[0]==replacement['sha256']
        # The authentic source remains pending, never rewritten to fake finish.
        snapshot=sqlite3.connect(Path(replacement['path']).as_uri()+'?mode=ro',uri=True)
        try:assert snapshot.execute('SELECT status FROM deletion_job WHERE deletion_id=?',(deletion,)).fetchone()[0]=='verifying'
        finally:snapshot.close()
        operation=restore_operation(client,headers=headers)
        result=signed_restore_post(client,headers=headers,json={'backup_id':replacement['backup_id'],
            'confirmation':'确认恢复','operation':operation})
        assert result.status_code==200,result.text
    if lane=='native_child_media':
        assert historical_checks=={'phase','marker','owner','current_parent_policy_still_strict'}
        record_property('historical_negative_controls',sorted(historical_checks))
        record_property('background_concurrency_credit',False)
    fresh=Database(restored.path,media_root=restored.path.parent,data_encryption_key=b'1'*32)
    assert PaperObjectiveBridge(fresh).read(CHILD,sent['receipt_id'])==sent
    with TestClient(create_app(settings,model_gateway=NoCloud()),headers={'X-HSC-Session':settings.session_token})as client:
        if lane=='native_child_media':_quiesce_original_visual(client)
        unlock=client.post('/v1/parent/unlock',json={'pin':'4826'});assert unlock.status_code==200,unlock.text
        headers={'X-HSC-Parent':unlock.json()['parent_token']}
        actual=client.app.state.privacy_lifecycle.database
        with actual.connect()as c:
            current_job=dict(c.execute('SELECT *FROM deletion_job WHERE deletion_id=?',(deletion,)).fetchone())
            assert current_job==live_job and current_job['status']=='completed'
            current=c.execute('SELECT *FROM privacy_operation_intent WHERE request_id=?',(action,)).fetchone()
            assert(dict(current)if current is not None else None)==live_intent
        health=client.get('/v1/parent/system-health',headers=headers);assert health.status_code==200,health.text
        assert health.json()['pending_deletion_jobs']==0
        retry=client.post(f'/v1/parent/deletions/{deletion}/retry',headers=headers,json={'confirmation':'确认删除'})
        assert retry.status_code in{200,409},retry.text
        record_property('completed_job_retry_http_status',retry.status_code)
        record_property('original_rotation_source_status','verifying')
        record_property('fresh_job_status',current_job['status'])
    exported=PrivacyLifecycleService(fresh,fresh.path.parent,b'1'*32).create_portable_backup(PASSPHRASE)
    assert Path(exported['path']).is_file()
    with fresh.connect()as c:assert not c.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('point',['before_prepare_journal','after_prepare_journal'])
def test_original_native_abort_after_parent_result_keeps_historical_work_bound(tmp_path,monkeypatch,record_property,point):
    from home_study_agent.parent_auth import ParentAuth
    from home_study_agent import parent_restore_ownership as ownership
    from test_native_privacy_permit_http import certificate,signed_header
    from test_controlled_launch_intent_http import KEY
    source=tmp_path/'source';source.mkdir(mode=0o700)
    db=Database(source/'agent.db',media_root=source,data_encryption_key=b'0'*32);db.initialize()
    ParentAuth(db).setup('4826')
    archive=PrivacyLifecycleService(db,source,b'0'*32).create_portable_backup(PASSPHRASE)['path']
    restored=_restore(tmp_path/'restored',archive,b'1'*32,quiesce_visual=True)
    settings=replace(_settings(restored.path.parent,b'1'*32),native_launch_attestation_key=KEY)
    observed=set();original_history=pub._historical_current_work;original_fault=pub._fault;faults=[]
    def observe_history(c,j,owner,generation):
        original_history(c,j,owner,generation)
        value=ownership._read_owner(owner)
        row,_,authority,_=pub._binding_facts(c,value['binding'])
        observed.add((value['state'],row['state'],pub._authority_status(authority)))
    monkeypatch.setattr(pub,'_historical_current_work',observe_history)
    with TestClient(create_app(settings,model_gateway=NoCloud()),headers={'X-HSC-Session':settings.session_token})as client:
        _quiesce_original_visual(client)
        unlocked=client.post('/v1/parent/unlock',json={'pin':'4826'});assert unlocked.status_code==200,unlocked.text
        headers={'X-HSC-Parent':unlocked.json()['parent_token']}
        backup=client.post('/v1/parent/backups',headers=headers);assert backup.status_code==200,backup.text
        with client.app.state.privacy_lifecycle.database.connect()as c:
            c.execute("UPDATE child_profile SET nickname='original current value'WHERE child_id=?",(CHILD,));c.commit()
        operation=restore_operation(client,headers=headers)
        body={'backup_id':backup.json()['backup_id'],'confirmation':'确认恢复','operation':operation}
        proof=certificate('/v1/parent/backups/restore',body,operation,kind='restore',child_id=None,scope=None)
        def stop(where):
            if where==point and not faults:faults.append(where);raise pub.RestorePublicationError('PUBLIC_BOUND_HISTORICAL_ABORT')
            return original_fault(where)
        monkeypatch.setattr(pub,'_fault',stop)
        result=client.post('/v1/parent/backups/restore',headers={**headers,**signed_header(proof)},json=body)
        assert result.status_code==409 and faults==[point],result.text
        monkeypatch.setattr(pub,'_fault',original_fault)
        status=client.get('/v1/parent/recovery-status',headers=headers)
        assert status.status_code==200 and status.json()['state']=='UNAPPLIED',status.text
        fenced=client.post('/v1/parent/native-privacy-permits/fence',headers=headers,json=proof)
        assert fenced.status_code==200 and fenced.json()['status']=='failed',fenced.text
        assert('ACTIVE','processing','claimed')in observed
        assert('CLEANED','failed','failed')in observed
        record_property('actual_historical_work_states',sorted(observed))
        record_property('background_concurrency_credit',False)
    with TestClient(create_app(settings,model_gateway=NoCloud()),headers={'X-HSC-Session':settings.session_token})as fresh:
        _quiesce_original_visual(fresh)
        assert fresh.get('/v1/health').json()['status']=='ok'
        actual=fresh.app.state.privacy_lifecycle.database
        with actual.connect()as c:
            row=c.execute('SELECT state,finalized,result_generation FROM privacy_operation_intent WHERE request_id=?',(operation['request_id'],)).fetchone()
            assert tuple(row)==('failed',0,None)
            assert c.execute('SELECT status FROM native_privacy_permit WHERE nonce=?',(proof['nonce'],)).fetchone()[0]=='failed'
            assert c.execute('SELECT nickname FROM child_profile WHERE child_id=?',(CHILD,)).fetchone()[0]=='original current value'
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
        with pub.publication_owner(actual,timeout=5)as owner:assert ownership.current(owner)is None


def test_real_second_encrypted_relocation_survives_old_roots_removed_without_hashing_other_child_media(tmp_path,monkeypatch,record_property):
    source=tmp_path/'source';source.mkdir(mode=0o700)
    db,_,_,_=fixture(source)
    from home_study_agent.parent_auth import ParentAuth
    ParentAuth(db).setup('4826')
    bridge=PaperObjectiveBridge(db)
    sent=bridge.submit(CHILD,**command(bridge.options(CHILD)))
    # A real original storage/OCR producer retains a large public synthetic
    # image for the other child. This is neither a file-existence flag nor a
    # database-only media registration.
    image=Image.frombytes('RGB',(1536,1536),os.urandom(1536*1536*3));stream=io.BytesIO();image.save(stream,format='PNG')
    raw=stream.getvalue();assert len(raw)>6*1024*1024
    other=ParentTextbookCaptureService(db,Adapter()).recognize_file(OTHER,base64.b64encode(raw).decode(),
        {'title':'public unrelated synthetic page','page_number':1},'public-relocation-other-page')
    other_id=ParentTextbookCaptureService(db,None)._id_for_ref(other['capture_ref'])
    assert ParentTextbookCaptureService(db,None)._read_raw_page(OTHER,other_id)==raw
    first=PrivacyLifecycleService(db,source,b'0'*32).create_portable_backup(PASSPHRASE)['path']
    one=_restore(tmp_path/'one',first,b'1'*32)
    assert PaperObjectiveBridge(one).read(CHILD,sent['receipt_id'])==sent
    second=PrivacyLifecycleService(one,one.path.parent,b'1'*32).create_portable_backup(PASSPHRASE)['path']
    two=_restore(tmp_path/'two',second,b'2'*32)
    shutil.rmtree(source);shutil.rmtree(one.path.parent)
    fresh=Database(two.path,media_root=two.path.parent,data_encryption_key=b'2'*32)
    assert ParentTextbookCaptureService(fresh,None)._read_raw_page(OTHER,other_id)==raw
    with pytest.raises(Exception):ParentTextbookCaptureService(fresh,None)._read_raw_page(CHILD,other_id)
    with fresh.connect()as c:
        other_path=Path(c.execute('SELECT raw_image_path FROM kg_parent_textbook_ocr_artifact WHERE artifact_id=?',(other_id,)).fetchone()[0])
        source_row=dict(c.execute('SELECT * FROM knowledge_source WHERE source_id=?',(sent['source_id'],)).fetchone())
        root=Path(c.execute("SELECT local_root_path FROM curriculum_standard_source WHERE source_id='std'").fetchone()[0])
        original_receipt=c.execute('SELECT summary_json FROM audit_event WHERE audit_id=?',(sent['receipt_id'],)).fetchone()[0]
    from home_study_agent.registered_asset_snapshot import HeldFile
    original_hash=HeldFile.hash;hashed=[]
    def hash_original(held):
        path=held.root.joinpath(*held.parts);assert path!=other_path,'unrelated large media must not be rehashed for a standard read'
        hashed.append(str(path));return original_hash(held)
    start=time.monotonic()
    with monkeypatch.context()as m:
        m.setattr(HeldFile,'hash',hash_original)
        assert PaperObjectiveBridge(fresh).read(CHILD,sent['receipt_id'])==sent
        with fresh.connect()as c:
            assert assert_source_current(c,None,CHILD,source_row)
        with pytest.raises(ValueError):PaperObjectiveBridge(fresh).read(OTHER,sent['receipt_id'])
    record_property('unrelated_original_bytes',len(raw));record_property('eligibility_seconds',time.monotonic()-start)
    assert any(Path(path).name==NAME for path in hashed)
    assert not source.exists()and not one.path.parent.exists()
    # In-place SQL edits remain unqualified and do not rewrite old audit/receipt.
    for sql,args in [
        ("UPDATE curriculum_standard_source SET title='changed material'WHERE source_id='std'",()),
        ("UPDATE curriculum_standard_source SET local_root_path=?WHERE source_id='std'",(str(tmp_path/'foreign'),)),
        ("UPDATE curriculum_standard_source SET local_root_path=?WHERE source_id='std'",(str(root)+'/.' ,)),
        ("UPDATE curriculum_standard_requirement SET title='changed requirement'WHERE requirement_id='req'",()),
        ("UPDATE curriculum_standard_page SET content_text=content_text||' changed'WHERE source_id='std'",())]:
        with fresh.connect()as c:
            c.execute('BEGIN');c.execute(sql,args)
            source_now=dict(c.execute('SELECT * FROM knowledge_source WHERE source_id=?',(sent['source_id'],)).fetchone())
            with pytest.raises((ValueError,pub.RestorePublicationError)):assert_source_current(c,None,CHILD,source_now)
            c.rollback()
    assert PaperObjectiveBridge(fresh).read(CHILD,sent['receipt_id'])==sent
    with fresh.connect()as c:
        assert c.execute('SELECT summary_json FROM audit_event WHERE audit_id=?',(sent['receipt_id'],)).fetchone()[0]==original_receipt
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
    # A fresh real App still consumes the unchanged receipt after both old
    # machines/roots are gone; no direct-reader-only substitute.
    settings=_settings(two.path.parent,b'2'*32)
    with TestClient(create_app(settings,model_gateway=NoCloud()),headers={'X-HSC-Session':settings.session_token})as client:
        unlocked=client.post('/v1/parent/unlock',json={'pin':'4826'});assert unlocked.status_code==200,unlocked.text
        h={'X-HSC-Parent':unlocked.json()['parent_token']}
        response=client.get(f'/v1/parent/knowledge-graph/textbook-capture/{CHILD}/objective-review/'+sent['receipt_id'],headers=h)
        assert response.status_code==200 and response.json()==sent,response.text
    # Each generation re-enrolled the same bounded explanation as a new owned
    # member, rather than overwriting the incoming capsule member.
    assert decode((root/NAME).read_bytes())[0]['old_root']!=str(root)
    # Real reads do not trust caller-replaced cursor methods or row factories.
    with fresh.connect()as c:
        c.execute('BEGIN')
        c.execute=lambda *a,**k:(_ for _ in ()).throw(AssertionError('caller SQL method used'))
        c.row_factory=lambda *a:'forged'
        assert assert_source_current(c,None,CHILD,source_row)
    # Reserved provenance cannot be exported by a standalone snapshot collector
    # or a foreign callback retaining the genuine producer's old ticket.
    from home_study_agent import registered_asset_backup as registered
    from home_study_agent import standard_relocation_provenance as provenance
    from home_study_agent.privacy_lifecycle import InvalidBackupError
    original_capsule=registered.create_capsule;captured={}
    def foreign_callback(snapshot,key,encrypt_file,**kwargs):
        ticket=kwargs['provenance_ticket'];state=provenance._EXPORTS[ticket]
        captured.update(ticket=ticket,snapshot=snapshot)
        return original_capsule(snapshot,key,encrypt_file,**kwargs)
    with monkeypatch.context()as m:
        m.setattr(registered,'create_capsule',foreign_callback)
        with pytest.raises(InvalidBackupError,match='REGISTERED_ASSET_SNAPSHOT_FAILED'):
            PrivacyLifecycleService(fresh,fresh.path.parent,b'2'*32).create_backup(prune=False)
    assert captured['ticket']not in provenance._EXPORTS
    with fresh.connect()as c:
        c.execute('BEGIN')
        with pytest.raises(StandardRelocationError):provenance.authorize_export(captured['ticket'],captured['snapshot'],'std',str(root))
    assert PaperObjectiveBridge(fresh).read(CHILD,sent['receipt_id'])==sent
    # A malformed/replaced/extra member is not converted into ordinary legacy
    # provenance, nor is the original receipt edited to hide the rejection.
    metadata=root/NAME;before=metadata.read_bytes();info=metadata.stat()
    metadata.write_bytes(b'{}')
    with pytest.raises((ValueError,pub.RestorePublicationError)):PaperObjectiveBridge(fresh).read(CHILD,sent['receipt_id'])
    metadata.write_bytes(before);os.utime(metadata,ns=(info.st_atime_ns,info.st_mtime_ns))
    extra=root/'unregistered-extra.bin';extra.write_bytes(b'public unrelated extra')
    with pytest.raises((ValueError,pub.RestorePublicationError)):PaperObjectiveBridge(fresh).read(CHILD,sent['receipt_id'])
    extra.unlink()
    assert PaperObjectiveBridge(fresh).read(CHILD,sent['receipt_id'])==sent
    # Unknown original close is retained and poisons admission. Do not reclose
    # its numeric descriptor or clear poison; this public synthetic test process
    # owns the residual until its original natural termination.
    original_enter=HeldFile.__enter__;original_close=os.close;debt={'fd':None,'calls':0}
    def enter(held):
        result=original_enter(held)
        if held.root.joinpath(*held.parts)==metadata:debt['fd']=held.fd
        return result
    def close(fd):
        if fd==debt['fd']and debt['fd']is not None:
            debt['calls']+=1
            raise OSError('public synthetic original close unconfirmed')
        return original_close(fd)
    with monkeypatch.context()as m:
        m.setattr(HeldFile,'__enter__',enter);m.setattr(os,'close',close)
        with pytest.raises(pub.RestorePublicationError,match='RESTORE_PUBLICATION_CLOSE_UNCONFIRMED'):
            PaperObjectiveBridge(fresh).read(CHILD,sent['receipt_id'])
    assert debt['calls']==1 and debt['fd']is not None
    assert os.fstat(debt['fd']).st_size==len(before)
    gate=pub._gate(pub._path(fresh.path))
    assert gate.poisoned and gate.retained and not fresh._original_connections
    record_property('original_unknown_close_retained',1)
