from local_restore_operation_fixture import restore_local
import asyncio
import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest
from normal_provider_owner import normal_provider_scope

from home_study_agent.database import Database
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.pet_audio_gateway import AudioEndpoint, PetAudioGateway
from home_study_agent.pet_audio_source import PetAudioAuthorityError
from home_study_agent.pet_audio_work import PetAudioTeachingService, quarantine_after_restore
from home_study_agent.learning_rubric import RubricEvidenceService
from test_pet_media_evidence import media, CHILD, change
from test_pet_audio_rubric_review import prepared
from test_pet_audio_gateway import feedback, stream


@pytest.fixture
def loopback():
    state = {'posts': [], 'mode': 'ok', 'callback': None, 'errors': []}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            try:
                raw=self.rfile.read(int(self.headers['Content-Length']))
                if not raw:
                    state['empty_requests']=state.get('empty_requests',0)+1
                    return
                value = json.loads(raw)
                original = base64.b64decode(value['messages'][1]['content'][1]['input_audio']['data'].split(',',1)[1])
                state['posts'].append({'sha256':hashlib.sha256(original).hexdigest(), 'stream':value['stream'], 'path':self.path})
                if state['callback']: state['callback']()
                body = stream(json.dumps(feedback()))
                self.send_response(429 if state['mode']=='429' else 200)
                self.send_header('Content-Type','text/event-stream')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                if state['mode']=='drop': body=body[:80]
                for start in range(0,len(body),7):
                    self.wfile.write(body[start:start+7])
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError): pass
            except BaseException as error:
                state['errors'].append(repr(error))
    server=HTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,name='pet-audio-loopback')
    thread.start()
    endpoint=AudioEndpoint(f'http://127.0.0.1:{server.server_port}/v1','synthetic-omni','synthetic',
        input_second_micros=10,input_text_token_micros=1,output_token_micros=1,tariff_version='synthetic-not-real-pricing')
    state['gateway']=PetAudioGateway(endpoint,allow_loopback=True)
    try: yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive() and not state['errors']


@pytest.fixture
def audio_case(media,loopback):
    db, rubric, assessment, _, prompt = prepared(media)
    # The helper fixture creates its own evaluation objective. Finalize that
    # synthetic runtime receipt exactly as the host does before oral delivery.
    with db.connect() as c:
        c.execute("UPDATE agent_objective SET status='completed' WHERE objective_id!=?",(assessment['objective_id'],))
        c.execute("UPDATE agent_turn SET tool_status='completed' WHERE agent_turn_id IN (SELECT agent_turn_id FROM agent_plan WHERE objective_id=?)",(assessment['objective_id'],))
        c.commit()
    privacy=PrivacyLifecycleService(db,media[4].parent)
    privacy.initialize()
    privacy.update_preferences({'cloud_audio_allowed':True})
    original=media[4].read_bytes()
    key=AESGCM.generate_key(bit_length=256)
    nonce=os.urandom(12)
    media[4].write_bytes(b'HSC1'+nonce+AESGCM(key).encrypt(nonce,original,b'hsc-raw-media-v1'))
    db=Database(db.path,media_root=media[4].parent,data_encryption_key=key)
    service=PetAudioTeachingService(db,media_root=media[4].parent,data_encryption_key=key,gateway=loopback['gateway'],runtime_epoch='first-process')
    grant=dict(allowed=True,expires_at=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
        day_limit_micros=20000,request_limit_micros=10000,expected_revision=0,action_id='allow-audio-purpose')
    service.grant(CHILD,**grant)
    with normal_provider_scope():
        yield service,assessment,prompt,grant,original,media


def enqueue(case):
    service,assessment,*_=case
    return service.enqueue(CHILD,assessment['assessment_id'])['work_id']


def test_encrypted_original_real_sse_saved_once_reopens_without_mastery(audio_case,loopback):
    service,assessment,_,_,original,media=audio_case
    wid=enqueue(audio_case)
    result=asyncio.run(service.run(CHILD,wid))
    assert result['state']=='response_saved', result
    assert loopback['posts']==[{'sha256':hashlib.sha256(original).hexdigest(),'stream':True,'path':'/v1/chat/completions'}]
    assert original not in media[4].read_bytes()
    restarted=PetAudioTeachingService(service.database,media_root=service.media_root,data_encryption_key=service.data_encryption_key,
        gateway=service.gateway,runtime_epoch='second-process')
    assert asyncio.run(restarted.run(CHILD,wid))==result
    assert len(loopback['posts'])==1
    assert not RubricEvidenceService(service.database).qualification(CHILD,assessment['assessment_id'])['score_qualified']
    with service.database.connect() as c:
        assert c.execute('SELECT count(*) FROM rubric_review_event').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==1


@pytest.mark.parametrize('mode',['drop','429'])
def test_unknown_never_reposts_on_same_key_or_restart(audio_case,loopback,mode):
    service=audio_case[0]
    wid=enqueue(audio_case)
    loopback['mode']=mode
    result=asyncio.run(service.run(CHILD,wid))
    assert result['state']=='unknown', result
    reopened=PetAudioTeachingService(service.database,media_root=service.media_root,data_encryption_key=service.data_encryption_key,
        gateway=service.gateway,runtime_epoch='new-process')
    assert asyncio.run(reopened.run(CHILD,wid))['state']=='unknown'
    assert len(loopback['posts'])==1
    with service.database.connect() as c:
        assert c.execute('SELECT state FROM pet_audio_dispatch').fetchone()[0]=='unknown'
    if mode=='drop':
        probe=dict(database=str(service.database.path),media=str(service.media_root),
            encryption_key=base64.b64encode(service.data_encryption_key).decode(),
            base_url=service.gateway.endpoint.base_url,child_id=CHILD,work_id=wid)
        code='''import asyncio,base64,json,os
from home_study_agent.database import Database
from home_study_agent.pet_audio_gateway import AudioEndpoint,PetAudioGateway
from home_study_agent.pet_audio_work import PetAudioTeachingService
from pathlib import Path
p=json.loads(os.environ['HSC_PET_RESTART_PROBE'])
key=base64.b64decode(p['encryption_key'])
endpoint=AudioEndpoint(p['base_url'],'synthetic-omni','synthetic',input_second_micros=10,input_text_token_micros=1,output_token_micros=1,tariff_version='synthetic-not-real-pricing')
db=Database(Path(p['database']),media_root=Path(p['media']),data_encryption_key=key)
service=PetAudioTeachingService(db,media_root=Path(p['media']),data_encryption_key=key,gateway=PetAudioGateway(endpoint,allow_loopback=True))
result=asyncio.run(service.run(p['child_id'],p['work_id']))
assert result['state']=='unknown' and result['result'] is None,result
print('PET_RESTART_UNKNOWN_NO_REPOST')
'''
        child=subprocess.run([sys.executable,'-c',code],env={**os.environ,
            'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src'),
            'HSC_PET_RESTART_PROBE':json.dumps(probe)},capture_output=True,text=True,timeout=30)
        assert child.returncode==0,child.stderr[-2000:]
        assert 'PET_RESTART_UNKNOWN_NO_REPOST' in child.stdout
        assert len(loopback['posts'])==1
    loopback['mode']='ok'
    reopened.retry(CHILD,wid,expected_generation=0,action_id='parent-explicit-retry',acknowledge_possible_charge=True)
    assert asyncio.run(reopened.run(CHILD,wid))['state']=='response_saved'
    assert len(loopback['posts'])==2


def test_concurrent_same_original_has_one_budget_and_one_post(audio_case,loopback):
    service=audio_case[0]
    wid=enqueue(audio_case)
    async def together(): return await asyncio.gather(service.run(CHILD,wid),service.run(CHILD,wid))
    results=asyncio.run(together())
    assert any(r['state']=='response_saved' for r in results), results
    assert len(loopback['posts'])==1
    with service.database.connect() as c:
        assert c.execute('SELECT count(*) FROM pet_audio_dispatch').fetchone()[0]==1


@pytest.mark.parametrize('fault',['grant','audio','child','switch','privacy','restore'])
def test_revoke_while_response_inflight_fences_save(audio_case,loopback,fault):
    service,_,_,grant,_,media=audio_case
    wid=enqueue(audio_case)
    def revoke():
        if fault=='grant': service.grant(CHILD,**{**grant,'allowed':False,'expected_revision':1,'action_id':'revoke'})
        elif fault=='audio': media[4].unlink()
        elif fault=='child': change(service.database,"UPDATE child_profile SET status='archived' WHERE child_id=?",(CHILD,))
        elif fault=='switch':
            from home_study_agent.repository import StudyRepository
            from home_study_agent.document_monitoring import DocumentMonitoringService
            DocumentMonitoringService(service.database)
            with service.database.connect() as c:
                target=c.execute("SELECT child_id FROM child_profile WHERE child_id!=? AND status='active' LIMIT 1",(CHILD,)).fetchone()[0]
                session=c.execute('SELECT session_id FROM pet_audio_work WHERE work_id=?',(wid,)).fetchone()[0]
            StudyRepository(service.database).switch_active_session_child(session,target,'switch-original-audio-owner')
        elif fault=='privacy': change(service.database,'UPDATE privacy_preference SET cloud_audio_allowed=0')
        else:
            with service.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                quarantine_after_restore(c)
                c.commit()
    loopback['callback']=revoke
    result=asyncio.run(service.run(CHILD,wid))
    expected_state = 'verification_pending' if fault == 'audio' else 'invalidated'
    assert result['state']==expected_state and result['result'] is None, result
    if fault == 'audio':
        assert result['source_verification'] == 'unavailable'
    assert len(loopback['posts'])==1
    with service.database.connect() as c:
        assert c.execute('SELECT result_json FROM pet_audio_work').fetchone()[0] is None


def test_cross_child_cannot_read_run_or_rebind(audio_case,loopback):
    service=audio_case[0]
    wid=enqueue(audio_case)
    for operation in (lambda:service.read('child-junior-3',wid),lambda:asyncio.run(service.run('child-junior-3',wid))):
        with pytest.raises(PetAudioAuthorityError,match='WORK_NOT_OWNED'): operation()
    assert not loopback['posts']


def test_stale_retry_cannot_dispatch_a_later_generation(audio_case,loopback):
    service=audio_case[0]
    wid=enqueue(audio_case)
    loopback['mode']='drop'
    asyncio.run(service.run(CHILD,wid))
    service.retry(CHILD,wid,expected_generation=0,action_id='retry-one',acknowledge_possible_charge=True)
    asyncio.run(service.run(CHILD,wid))
    service.retry(CHILD,wid,expected_generation=1,action_id='retry-two',acknowledge_possible_charge=True)
    result=asyncio.run(service.run(CHILD,wid,expected_generation=1))
    assert result['state']=='queued' and result['generation']==2
    assert len(loopback['posts'])==2
    with service.database.connect() as c:
        assert c.execute('SELECT count(*) FROM pet_audio_dispatch').fetchone()[0]==2


def test_actual_backup_restore_never_restores_audio_permission_or_delivery(audio_case,loopback):
    service=audio_case[0]
    wid=enqueue(audio_case)
    assert asyncio.run(service.run(CHILD,wid))['state']=='response_saved'
    privacy=PrivacyLifecycleService(service.database,service.media_root.parent,service.data_encryption_key)
    backup=privacy.create_backup(prune=False)
    with service.database.connect() as c:
        c.execute("UPDATE daily_session SET status='completed' WHERE status='active'")
        c.commit()
    restore_local(privacy,backup['backup_id'],'确认恢复')
    reopened=PetAudioTeachingService(service.database,media_root=service.media_root,
        data_encryption_key=service.data_encryption_key,gateway=service.gateway,runtime_epoch='restored-process')
    current=reopened.read(CHILD,wid)
    assert current['state']=='invalidated' and current['result'] is None
    with service.database.connect() as c:
        with pytest.raises(PetAudioAuthorityError): reopened._grant(c,CHILD)
    assert asyncio.run(reopened.run(CHILD,wid))['state']=='invalidated'
    assert len(loopback['posts'])==1


def test_media_scope_erases_work_but_retains_anonymous_unknown_cost(audio_case,loopback):
    service=audio_case[0]
    wid=enqueue(audio_case)
    loopback['mode']='drop'
    assert asyncio.run(service.run(CHILD,wid))['state']=='unknown'
    privacy=PrivacyLifecycleService(service.database,service.media_root.parent)
    privacy.delete_child_scope(CHILD,'media','确认删除','erase-audio-media')
    with service.database.connect() as c:
        assert not c.execute('SELECT 1 FROM pet_audio_work WHERE child_id=?',(CHILD,)).fetchone()
        assert not c.execute('SELECT 1 FROM pet_audio_grant WHERE child_id=?',(CHILD,)).fetchone()
        row=c.execute('SELECT * FROM pet_audio_dispatch').fetchone()
        assert row['child_id'] is None and row['work_id'] is None and row['state']=='unknown' and row['cost_micros']>0
    assert len(loopback['posts'])==1


def test_result_without_a_consumed_dispatch_cannot_be_saved(audio_case):
    service=audio_case[0]
    wid=enqueue(audio_case)
    claim=service._claim(CHILD,wid)
    with pytest.raises(PetAudioAuthorityError,match='DISPATCH'):
        service._save(CHILD,wid,claim,{'feedback':feedback(),'reported_model':'synthetic-omni','requested_model':'synthetic-omni'})


def test_expired_owner_cannot_save_and_same_key_never_reposts(audio_case,loopback):
    service=audio_case[0]
    wid=enqueue(audio_case)
    claim=service._claim(CHILD,wid)
    service._authorize(CHILD,wid,claim,'headers')
    service._authorize(CHILD,wid,claim,'body')
    change(service.database,"UPDATE pet_audio_work SET lease_until='2000-01-01T00:00:00+00:00'")
    with pytest.raises(PetAudioAuthorityError,match='OWNER'):
        service._save(CHILD,wid,claim,{'feedback':feedback(),'reported_model':'synthetic-omni','requested_model':'synthetic-omni'})
    assert asyncio.run(service.run(CHILD,wid))['state']=='unknown'
    assert not loopback['posts']


def test_unknown_cost_hold_blocks_another_paid_retry(audio_case,loopback):
    service,assessment,_,grant,*_=audio_case
    with service.database.connect() as c:
        c.execute('BEGIN')
        original=service._source(c,CHILD,assessment['assessment_id'],live=True)
    cost=service.gateway.endpoint.reserve_cost(duration_ms=original['binding']['duration_ms'],question=original['question'])
    service.grant(CHILD,**{**grant,'day_limit_micros':cost,'request_limit_micros':cost,'expected_revision':1,'action_id':'strict-budget'})
    wid=enqueue(audio_case)
    loopback['mode']='429'
    assert asyncio.run(service.run(CHILD,wid))['state']=='unknown'
    service.retry(CHILD,wid,expected_generation=0,action_id='retry',acknowledge_possible_charge=True)
    with pytest.raises(PetAudioAuthorityError,match='BUDGET_EXHAUSTED'): asyncio.run(service.run(CHILD,wid))
    assert len(loopback['posts'])==1


def test_revoke_after_headers_prevents_original_audio_body(audio_case,loopback,monkeypatch):
    service,_,_,grant,*_=audio_case
    wid=enqueue(audio_case)
    authorize=service._authorize
    phases=[]
    def gate(child,work,claim,phase):
        phases.append(phase)
        authorize(child,work,claim,phase)
        if phase=='headers':
            service.grant(CHILD,**{**grant,'allowed':False,'expected_revision':1,'action_id':'revoke-before-body'})
    monkeypatch.setattr(service,'_authorize',gate)
    result=asyncio.run(service.run(CHILD,wid))
    assert phases==['headers','body']
    assert result['state']=='invalidated' and result['result'] is None
    assert not loopback['posts']
    with service.database.connect() as c:
        assert c.execute('SELECT state FROM pet_audio_dispatch').fetchone()[0]=='unknown'


def test_expired_old_result_is_fenced_after_new_generation_saved(audio_case,loopback):
    service=audio_case[0]
    wid=enqueue(audio_case)
    old=service._claim(CHILD,wid)
    service._authorize(CHILD,wid,old,'headers')
    service._authorize(CHILD,wid,old,'body')
    change(service.database,"UPDATE pet_audio_work SET lease_until='2000-01-01T00:00:00+00:00'")
    service.retry(CHILD,wid,expected_generation=0,action_id='replace-expired',acknowledge_possible_charge=True)
    saved=asyncio.run(service.run(CHILD,wid,expected_generation=1))
    assert saved['state']=='response_saved' and saved['generation']==1
    with pytest.raises(PetAudioAuthorityError,match='OWNER_FENCED'):
        service._save(CHILD,wid,old,{'feedback':feedback(),'reported_model':'obsolete','requested_model':'synthetic-omni'})
    assert service.read(CHILD,wid)==saved
    assert len(loopback['posts'])==1


def test_stale_service_cannot_send_after_current_endpoint_config_changes(audio_case,loopback):
    service=audio_case[0]
    wid=enqueue(audio_case)
    change(service.database,"UPDATE pet_audio_config SET endpoint_hash='reconfigured'")
    with pytest.raises(PetAudioAuthorityError,match='PURPOSE_CHANGED'):
        asyncio.run(service.run(CHILD,wid))
    assert not loopback['posts']


def test_corrupt_saved_feedback_is_not_returned_as_invalidated_payload(audio_case,loopback):
    service=audio_case[0]
    wid=enqueue(audio_case)
    asyncio.run(service.run(CHILD,wid))
    change(service.database,'UPDATE pet_audio_work SET result_json=?',(json.dumps({'feedback':{'mastery':True}}),))
    current=service.read(CHILD,wid)
    assert current['state']=='invalidated' and current['result'] is None
