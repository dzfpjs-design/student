"""Original encrypted synthetic PCM survives restore; old input stays quarantined.

Only recognition/model outputs are synthetic. HTTP capture, HSC1 publication,
answer provenance, candidate writes, native-proof restore and reopen are real.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.database import Database, LATEST_SCHEMA_VERSION
from home_study_agent.pet_media_evidence import _read_audio
from home_study_agent.pet_multipa_candidate import prepare_input
from home_study_agent.pet_speaking_candidate_store import SpeakingModelCandidateStore, TABLES
from test_agent_continuation import Gateway
from test_diagnostic_prompt_http import environment, start, CHILD
from test_pet_media_evidence import local_review
from test_pet_speaking_model_candidates import ASSETS, output
from test_period_rebinding_backup_restore import restore_operation
from test_voice_runtime_binding import capture, interpret, submit
from synthetic_native_restore_fixture import KEY, signed_local_restore_post


def _rows(db):
    with db.connect() as c:
        return {t: [tuple(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in TABLES}


def test_encrypted_original_voice_candidate_restore_reopen_preserves_history_not_input_authority(environment, monkeypatch):
    settings, _ = environment
    settings = replace(settings, data_encryption_key=b'R'*32, native_launch_attestation_key=KEY)
    db = Database(settings.database_path, media_root=settings.data_dir,
                  data_encryption_key=settings.data_encryption_key)
    gateway = Gateway({}); gateway.configured = False
    answer = 'I like reading in the library because it is quiet. I read story books there after school with my friends.'
    store = SpeakingModelCandidateStore(db, media_root=settings.data_dir,
                                      data_encryption_key=settings.data_encryption_key)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        token = client.post('/v1/parent/setup', json={'pin':'7391'}).json()['parent_token']
        client.headers['X-HSC-Parent'] = token
        assert client.put('/v1/parent/privacy', json={'raw_media_storage_allowed':True}).status_code == 200
        prepared = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
            'track':'pet', 'preferred_item_id':'diag-pet-speaking-2', 'client_action_id':'backup-question'})
        assert prepared.status_code == 200, prepared.text
        prompt = prepared.json()['prompt']
        voice = capture(client, settings, monkeypatch, sid, answer, prompt, 'original-audio')
        public_pcm = (settings.data_dir/'synthetic-runtime.wav').read_bytes()
        with db.connect() as c:
            source = dict(c.execute('SELECT * FROM voice_input WHERE voice_input_id=?',(voice,)).fetchone())
            assert source['child_id'] == CHILD and source['session_id'] == sid
            assert Path(source['local_path']).read_bytes().startswith(b'HSC1')
            assert _read_audio(source['local_path'], media_root=settings.data_dir,
                               data_encryption_key=settings.data_encryption_key) == public_pcm
        heard = interpret(client, sid, answer, 'original-answer-turn', voice)
        assert heard['intent'] == 'pending_answer_clarification'
        assert client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute").status_code == 200
        confirmation = '刚才那句就是我的答案'
        confirming_voice = capture(client, settings, monkeypatch, sid, confirmation, prompt, 'confirmation-audio')
        heard = interpret(client, sid, confirmation, 'confirmation-turn', confirming_voice)
        assert heard['action']['source_voice_input_id'] == voice
        answered = submit(client, sid, prompt, heard, answer=answer)
        assert answered.status_code == 200, answered.text
        assessment = local_review((db,None,sid,None,settings.data_dir/'synthetic-runtime.wav',None),
                                  prompt,answer,approve=False,scored=answered.json())
        collection = client.get(f"/v1/sessions/{sid}/pet-media/prompt/{prompt['prompt_id']}",
                                params={'item_version':prompt['item_version']})
        assert collection.status_code == 200, collection.text
        bindings = collection.json()['speaking']
        assert len(bindings) == 1 and bindings[0]['voice_input_id'] == voice
        with db.connect() as c:
            binding, actual_pcm = prepare_input(c,sid,media_id=bindings[0]['media_id'],
                assessment_id=assessment['assessment_id'], media_root=settings.data_dir,
                data_encryption_key=settings.data_encryption_key)
        assert actual_pcm == public_pcm
        request = dict(binding=binding,result=output(),asset_hashes=ASSETS)
        recorded = store.record(CHILD,sid,**request,client_action_id='candidate-before-backup')
        cid = recorded['current']['candidate_id']
        assert recorded['current']['source_status'] == 'current'
        withdrawn = store.record(CHILD,sid,**request,client_action_id='withdrawn-candidate-before-backup')
        wid = withdrawn['current']['candidate_id']
        store.withdraw(CHILD,wid,expected_revision=1,client_action_id='withdraw-before-backup')
        before = _rows(db)
        # Original session commands satisfy restore's no-active-session gate;
        # no direct SQL status update or fabricated restore owner/lease.
        for old,new in (('check_in','classroom_recall'),('classroom_recall','homework_capture'),
                        ('homework_capture','plan_review'),('plan_review','study'),('study','reflection')):
            if old == 'classroom_recall':
                assert client.post(f'/v1/sessions/{sid}/recalls/none',json={'client_action_id':'no-recall'}).status_code == 200
            if old == 'homework_capture':
                assert client.post(f'/v1/sessions/{sid}/homework/none',json={'client_action_id':'no-homework'}).status_code == 200
            moved = client.post(f'/v1/sessions/{sid}/transition',json={
                'expected_state':old,'target_state':new,'reason':'public synthetic restore fixture',
                'client_action_id':'close-for-backup-'+new})
            assert moved.status_code == 200, moved.text
        closed = client.post(f'/v1/sessions/{sid}/reflection/complete',json={
            'learned_text':'Synthetic recording persistence only','difficult_text':'','feeling':'okay',
            'next_action':'','skipped':False,'client_action_id':'finish-before-backup'})
        assert closed.status_code == 200, closed.text
        backup = client.post('/v1/parent/backups')
        assert backup.status_code == 200, backup.text
        restored = signed_local_restore_post(client,json={'backup_id':backup.json()['backup_id'],
            'confirmation':'确认恢复','operation':restore_operation(client,{})})
        assert restored.status_code == 200, restored.text
        assert restored.json()['status'] == 'restored'
    # Fresh App and Database, not the writer's in-memory candidate or file.
    reopened = Database(settings.database_path,media_root=settings.data_dir,
                        data_encryption_key=settings.data_encryption_key)
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        client.headers['X-HSC-Parent'] = client.post('/v1/parent/unlock',json={'pin':'7391'}).json()['parent_token']
        assert _rows(reopened) == before
        history = SpeakingModelCandidateStore(reopened,media_root=settings.data_dir,
                                              data_encryption_key=settings.data_encryption_key)
        current = history.get(CHILD,cid)
        assert current['binding'] == binding
        assert current['source_status'] == 'changed' and current['status'] == 'changed'
        assert not current['skill_qualified'] and not current['mastery_claim_allowed']
        assert history.get(CHILD,wid)['status'] == 'withdrawn'
        assert [r['operation'] for r in history.history(CHILD,wid)] == ['recorded','withdrawn']
        with reopened.connect() as c:
            raw = c.execute('SELECT local_path FROM voice_input WHERE voice_input_id=? AND child_id=? AND session_id=?',
                            (voice,CHILD,sid)).fetchone()
            assert raw and _read_audio(raw[0],media_root=settings.data_dir,
                                      data_encryption_key=settings.data_encryption_key) == public_pcm
            capture_row = c.execute('SELECT status,wav_sha256,question_validation FROM voice_capture_binding WHERE voice_input_id=?',
                                    (voice,)).fetchone()
            assert tuple(capture_row) == ('cancelled',hashlib.sha256(public_pcm).hexdigest(),'valid')
            assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == LATEST_SCHEMA_VERSION
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
            assert c.execute('SELECT count(*) FROM model_call').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM rubric_review_event').fetchone()[0] == 0
    assert not gateway.requests
