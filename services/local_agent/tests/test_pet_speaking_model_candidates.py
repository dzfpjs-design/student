"""Real isolated schema89/current Database; synthetic numeric outputs only."""
from pathlib import Path
from uuid import uuid4
import hashlib
import json
import sqlite3

import pytest
from home_study_agent.database import Database, LATEST_SCHEMA_VERSION
from home_study_agent.pet_speaking_candidate_store import SpeakingModelCandidateStore, TABLES, ensure_schema
from home_study_agent.pet_multipa_candidate import prepare_input
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_pet_media_evidence import media, change, CHILD
from test_pet_speaking_review import setup

ASSETS={k:hashlib.sha256(('SYNTHETIC-NOT-A-MODEL-'+k).encode()).hexdigest() for k in
 ('multipa','charsiu','hubert','roberta','whisper_medium_en','whisper_base_en')}
def output():
    return dict(uttr_acc=1.25,uttr_fluency=2.5,uttr_prosodic=-0.5,uttr_total=1,
        word_acc=[1.0],word_stress=[0.5],word_total=[0.75],word_text=['DO_NOT_PERSIST_WORD'],
        transcript_S='DO_NOT_PERSIST_TRANSCRIPT_S',transcript_W='DO_NOT_PERSIST_TRANSCRIPT_W')

def prepared(media):
    _,args=setup(media);service=SpeakingModelCandidateStore(media[0],media_root=media[4].parent)
    with media[0].connect()as c:binding,_=prepare_input(c,media[2],**args)
    request=dict(binding=binding,result=output(),asset_hashes=ASSETS,client_action_id='record-one')
    return service,args,request


def test_numeric_only_durable_restart_idempotency_withdrawal_cannot_revive(media):
    service,args,request=prepared(media);first=service.record(CHILD,media[2],**request);identifier=first['current']['candidate_id']
    assert first['current']['status']=='current'and first['current']['revision']==1
    assert not first['current']['skill_qualified']and not first['current']['assessor_installed']
    assert service.record(CHILD,media[2],**request)==first
    reopened=SpeakingModelCandidateStore(Database(media[0].path),media_root=media[4].parent)
    assert reopened.get(CHILD,identifier)==first['current']
    with media[0].connect()as c:
        dump='\n'.join(c.iterdump())
        assert 'DO_NOT_PERSIST_'not in dump
        assert c.execute('SELECT count(*)FROM rubric_review_event').fetchone()[0]==0
        assert c.execute('SELECT count(*)FROM model_call').fetchone()[0]==0
        assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==LATEST_SCHEMA_VERSION
    with pytest.raises(ValueError,match='REVISION_CONFLICT'):reopened.withdraw(CHILD,identifier,expected_revision=2,client_action_id='wrong-cas')
    withdrawn=reopened.withdraw(CHILD,identifier,expected_revision=1,client_action_id='withdraw-one')
    assert withdrawn['current']['status']=='withdrawn'
    assert reopened.withdraw(CHILD,identifier,expected_revision=1,client_action_id='withdraw-one')==withdrawn
    replay=reopened.record(CHILD,media[2],**request)
    assert replay['receipt']==first['receipt']and replay['current']['status']=='withdrawn'
    assert [e['operation']for e in reopened.history(CHILD,identifier)]==['recorded','withdrawn']
    with pytest.raises(ValueError,match='IDEMPOTENCY_CONFLICT'):
        reopened.record(CHILD,media[2],**{**request,'result':{**output(),'uttr_acc':9}})
    for call in [lambda:reopened.get('child-junior-3',identifier),lambda:reopened.withdraw('child-junior-3',identifier,expected_revision=1,client_action_id='cross-child')]:
        with pytest.raises(ValueError,match='NOT_FOUND'):call()


@pytest.mark.parametrize('fault',['audio','transcript','item','cancel'])
def test_source_changed_keeps_history_but_refuses_new_candidate(media,fault):
    service,args,request=prepared(media);first=service.record(CHILD,media[2],**request);identifier=first['current']['candidate_id']
    if fault=='audio':media[4].write_bytes(b'not-original-audio')
    elif fault=='transcript':change(media[0],"UPDATE voice_input SET transcript='changed words'")
    elif fault=='item':change(media[0],"UPDATE diagnostic_prompt SET item_version='changed'")
    else:change(media[0],"UPDATE pet_media_binding SET state_json=json_set(state_json,'$.status','cancelled')")
    assert service.get(CHILD,identifier)['status']=='changed'
    assert service.record(CHILD,media[2],**request)['current']['status']=='changed'
    with pytest.raises(ValueError):service.record(CHILD,media[2],**{**request,'client_action_id':'new-after-change'})
    assert service.withdraw(CHILD,identifier,expected_revision=1,client_action_id='withdraw-changed')['current']['status']=='withdrawn'


def test_insert_event_failure_rolls_back_and_immutable_direct_writes_reject(media):
    service,args,request=prepared(media)
    with media[0].connect()as c:
        c.execute("CREATE TRIGGER synthetic_event_fault BEFORE INSERT ON pet_speaking_model_candidate_event BEGIN SELECT RAISE(ABORT,'SYNTHETIC_DISK_EVENT_FAILURE');END");c.commit()
    with pytest.raises(sqlite3.IntegrityError,match='SYNTHETIC_DISK_EVENT_FAILURE'):service.record(CHILD,media[2],**request)
    with media[0].connect()as c:
        assert c.execute('SELECT count(*)FROM '+TABLES[0]).fetchone()[0]==0
        c.execute('DROP TRIGGER synthetic_event_fault');c.commit()
    saved=service.record(CHILD,media[2],**request)
    for table in TABLES:
        with media[0].connect()as c:
            with pytest.raises(sqlite3.IntegrityError,match='IMMUTABLE'):c.execute('UPDATE '+table+' SET child_id=child_id')
    with media[0].connect()as c:
        with pytest.raises(sqlite3.IntegrityError,match='PRIVACY_DELETE_REQUIRED'):c.execute('DELETE FROM '+TABLES[0])
    assert service.get(CHILD,saved['current']['candidate_id'])['status']=='current'


def test_migration_88_to_89_backup_and_schema_ddl_borrowed_rollback(media):
    service,args,request=prepared(media)
    def original_empty_88(c):
        # Undo *all* post88 additions, including the later empty job table and
        # its automatically removed index/trigger. Declaring 88 while leaving
        # a future90/91 table is not a predecessor database.
        # A declared88 predecessor cannot retain empty future92 CPU receipts.
        # Never erase receipt history to simulate an older database.
        from home_study_agent.pet_multipa_execution_receipt import OBJECTS, validate_schema as validate_receipts
        validate_receipts(c)
        for kind, name, _ in reversed(OBJECTS):
            if kind == 'table':
                assert c.execute('SELECT count(*)FROM '+name).fetchone()[0] == 0
            c.execute('DROP '+kind.upper()+' '+name)
        assert not c.execute('SELECT 1 FROM sqlite_master WHERE name IN ('+','.join('?' for _ in OBJECTS)+')', tuple(row[1] for row in OBJECTS)).fetchall()
        table='pet_speaking_candidate_job'
        if c.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone():
            assert c.execute('SELECT count(*)FROM '+table).fetchone()[0]==0
            c.execute('DROP TABLE '+table)
        for table in reversed(TABLES):
            assert c.execute('SELECT count(*)FROM '+table).fetchone()[0]==0
            c.execute('DROP TABLE '+table)
        c.execute('DELETE FROM schema_migration WHERE version>=89');c.commit()
        assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==88
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'pet_speaking_%'").fetchall()
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
    with media[0].connect()as c:original_empty_88(c)
    current=Database(media[0].path);current.initialize()
    with current.connect()as c:
        assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==LATEST_SCHEMA_VERSION
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
    upgrade=current.latest_upgrade_status()
    assert upgrade['status']=='completed'and upgrade['snapshot_available']
    assert (upgrade['source_schema_version'],upgrade['target_schema_version'])==(88,LATEST_SCHEMA_VERSION)
    with media[0].connect()as c:
        original_empty_88(c)
        c.execute('BEGIN');ensure_schema(c);assert c.in_transaction;c.rollback()
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name=?",(TABLES[0],)).fetchone()is None
        assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==88
        # Retain the original historical89 contract separately: install only
        # its two immutable candidate tables under the caller's transaction.
        ensure_schema(c);c.execute("INSERT INTO schema_migration(version,applied_at) VALUES (89,datetime('now'))");c.commit()
        from home_study_agent.pet_speaking_candidate_store import validate_schema
        validate_schema(c)
        assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==89
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='pet_speaking_candidate_job'").fetchone()
    recorded=service.record(CHILD,media[2],**request)
    current.initialize()
    assert service.get(CHILD,recorded['current']['candidate_id'])==recorded['current']
    assert current.latest_upgrade_status()['source_schema_version']==89


def test_real_privacy_export_delete_learning_preserves_other_child(media):
    service,args,request=prepared(media);saved=service.record(CHILD,media[2],**request)
    privacy=PrivacyLifecycleService(media[0],media[0].path.parent);privacy.initialize()
    exported=privacy.export_child(CHILD)
    export_data=json.loads(Path(exported['path']).read_text())
    assert len(export_data['pet_speaking_model_candidates'])==1
    assert 'DO_NOT_PERSIST_'not in json.dumps(export_data)
    deleted=privacy.delete_child_scope(CHILD,'learning','确认删除','erase-acoustic-candidate')
    assert deleted['status']=='completed'
    with media[0].connect()as c:
        assert all(c.execute('SELECT count(*)FROM '+t).fetchone()[0]==0 for t in TABLES)
        assert c.execute("SELECT 1 FROM child_profile WHERE child_id='child-junior-3'").fetchone()
        assert not c.execute('PRAGMA foreign_key_check').fetchall()



from test_period_rebinding_backup_restore import (
    journey, flow, learning_clock, consistent_action_clock, KEY,
    test_current_schema_backup_restore_restart_original_command_and_withdrawal as original_restore)


@pytest.mark.parametrize('journey',[KEY],indirect=True)
def test_schema89_two_immutable_candidate_tables_follow_original_backup_restore(journey):
    f,settings,client,headers=journey
    with f.db.connect()as c:
        c.execute('INSERT INTO pet_speaking_model_candidate VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                  ('schema-only-witness','child-primary-4',f.sid,'unavailable-schema-media','unavailable-schema-assessment',
                   json.dumps({'fixture':'schema-only-not-valid-audio-authority'}),
                   json.dumps({'sentence':{'uttr_acc':1.25},'words':{'word_acc':[1.0]},'word_count':1}),
                   'yuwchen/MultiPA:research-candidate','{}','a'*64,'2026-10-24T00:00:00+00:00'))
        c.execute('INSERT INTO pet_speaking_model_candidate_event VALUES (?,?,?,?,?,?,?,?)',
                  ('schema-only-event','schema-only-witness','child-primary-4',1,'recorded','schema-only-action','a'*64,'2026-10-24T00:00:00+00:00'))
        c.commit();before={t:[tuple(r)for r in c.execute('SELECT *FROM '+t+' ORDER BY rowid')]for t in TABLES}
    # Entire original real HTTP+DB restore/reopen/CAS/retraction assertions run.
    original_restore(journey,False)
    with f.db.connect()as c:
        assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==LATEST_SCHEMA_VERSION
        assert {t:[tuple(r)for r in c.execute('SELECT *FROM '+t+' ORDER BY rowid')]for t in TABLES}==before
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('scope',['media','all_history'])
def test_original_privacy_lifecycle_removes_candidate_and_events(media,scope):
    service,args,request=prepared(media);service.record(CHILD,media[2],**request)
    privacy=PrivacyLifecycleService(media[0],media[0].path.parent);privacy.initialize()
    answer=privacy.delete_child_scope(CHILD,scope,'确认删除','privacy-'+scope)
    assert answer['status']=='completed'
    with media[0].connect()as c:
        assert all(c.execute('SELECT count(*)FROM '+t).fetchone()[0]==0 for t in TABLES)
        assert c.execute("SELECT 1 FROM child_profile WHERE child_id='child-junior-3'").fetchone()
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


def test_original_parent_delete_order_cascades_schema_witness(tmp_path):
    db=Database(tmp_path/'schema-only.db');db.initialize()
    with db.connect()as c:
        c.execute("INSERT INTO child_profile VALUES ('schema-child','synthetic','4','synthetic','active','2026-10-24','2026-10-24')")
        c.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)VALUES('schema-session','schema-child','completed','closed','2026-10-24','2026-10-24','schema-session-action')")
        c.execute('INSERT INTO pet_speaking_model_candidate VALUES (?,?,?,?,?,?,?,?,?,?,?)',
          ('schema-candidate','schema-child','schema-session','schema-media','schema-assessment','{}','{}','schema-witness','{}','a'*64,'2026-10-24'))
        c.execute('INSERT INTO pet_speaking_model_candidate_event VALUES (?,?,?,?,?,?,?,?)',
          ('schema-event','schema-candidate','schema-child',1,'recorded','schema-action','a'*64,'2026-10-24'))
        c.commit()
        assert c.execute('PRAGMA foreign_keys').fetchone()[0]==1
        assert all(r['on_delete']=='CASCADE'for t in TABLES for r in c.execute('PRAGMA foreign_key_list('+t+')'))
        # The original session->child constraint intentionally rejects this order.
        with pytest.raises(sqlite3.IntegrityError):c.execute("DELETE FROM child_profile WHERE child_id='schema-child'")
        c.rollback()
        c.execute("DELETE FROM daily_session WHERE session_id='schema-session'")
        assert all(c.execute('SELECT count(*)FROM '+t).fetchone()[0]==0 for t in TABLES)
        c.execute("DELETE FROM child_profile WHERE child_id='schema-child'");c.commit()
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


def test_actual_concurrent_once_record_and_one_winning_withdrawal(media):
    from concurrent.futures import ThreadPoolExecutor
    service,args,request=prepared(media)
    with ThreadPoolExecutor(max_workers=2)as pool:
        results=list(pool.map(lambda _:service.record(CHILD,media[2],**request),range(2)))
    assert results[0]==results[1]
    identifier=results[0]['current']['candidate_id']
    def withdraw(key):
        try:return service.withdraw(CHILD,identifier,expected_revision=1,client_action_id=key)
        except ValueError as e:return str(e)
    with ThreadPoolExecutor(max_workers=2)as pool:values=list(pool.map(withdraw,['race-a','race-b']))
    assert sum(type(v)is dict for v in values)==1
    assert sum(v=='PET_ACOUSTIC_CANDIDATE_REVISION_CONFLICT'for v in values)==1
    assert len(service.history(CHILD,identifier))==2


@pytest.mark.parametrize('fault',['deep','wide','text','integer','nan'])
def test_rejected_payload_is_bounded_before_action_hash_or_persistence(media,fault):
    service,args,request=prepared(media);bad=dict(request);result=dict(output())
    if fault=='deep':result['extra']={'a':{'b':{'c':{'d':1}}}}
    elif fault=='wide':result['word_acc']=[1]*513
    elif fault=='text':result['transcript_S']='x'*16001
    elif fault=='integer':result['uttr_acc']=10**2000
    else:result['uttr_acc']=float('nan')
    bad['result']=result
    with pytest.raises(ValueError,match='REQUEST_BOUND'):service.record(CHILD,media[2],**bad)
    with media[0].connect()as c:
        assert all(c.execute('SELECT count(*)FROM '+t).fetchone()[0]==0 for t in TABLES)
