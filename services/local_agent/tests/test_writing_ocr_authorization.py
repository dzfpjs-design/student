"""Synthetic exact pages and confirmation speech, never real child evidence."""
from datetime import datetime,timezone
import json
from uuid import uuid4

import pytest

from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
from home_study_agent.writing_ocr_authorization import WritingOcrAuthorizationService,arrival_binding
from home_study_agent.writing_response_origin import bind_written_attempt,original_written_response
from test_writing_response_origin import writing,CHILD,TEXT
from test_pet_media_evidence import wav_file


@pytest.fixture
def ocr(writing):
    db,sid,_,p,_ = writing
    # Isolate the real document producer from unrelated daily-plan setup.
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,))
        c.commit()
    monitor = DocumentMonitoringService(db)
    monitor.start(sid,'monitor')
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as c:
        c.execute("INSERT INTO capture_artifact VALUES('page',?,?,'document_ocr',NULL,?,.95,'{}','completed',?,'page')",(CHILD,sid,TEXT,now))
        c.commit()
    revision = monitor.record_revision(sid,'page','fixture-page','revision')
    service = WritingOcrAuthorizationService(db,runtime_epoch='process-one')
    service.initialize()
    packet = service.prepare_confirmation(sid,prompt_id=p['prompt_id'],item_version=p['item_version'],artifact_id='page',
        revision_id=revision['revision_id'],response_start=0,response_end=len(TEXT),client_action_id='prepare')
    return writing,service,packet


def readback(ocr,event='started',playback='read-one',key=None):
    writing,service,p = ocr
    return service.record_readback(writing[1],confirmation_id=p['confirmation_id'],binding_version=p['binding_version'],
        playback_id=playback,event=event,readback_sha256=p['readback_sha256'],client_action_id=key or event)


def confirm_turn(ocr, transcript='是的'):
    writing,_,p = ocr
    db,sid,_,prompt,_ = writing
    path = db.path.parent/'confirmation.wav'
    wav_file(path)
    capture = VoiceCaptureBindingService(db)
    capture.begin(sid,'confirmation-voice',path.read_bytes(),'pet_speaking',
        {'child_id':CHILD,'question_reference':{k:prompt[k] for k in ('prompt_id','item_version')}})
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as c:
        c.execute("INSERT INTO voice_input VALUES('confirm-voice',?,?,'pet_speaking',?,?,'synthetic-no-ASR',1000,'completed',?,'confirmation-voice')",(CHILD,sid,transcript,str(path),now))
        c.execute("INSERT INTO voice_transcription_action VALUES('confirmation-voice',?,?,'completed',?)",(sid,CHILD,now))
        c.commit()
    capture.complete(sid,'confirmation-voice','confirm-voice')
    objective,turn,plan,step = [uuid4().hex for _ in range(4)]
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as c:
        frozen = arrival_binding(c,session_id=sid,confirmation_id=p['confirmation_id'],binding_version=p['binding_version'],voice_input_id='confirm-voice')
        action = {'type':'confirm_writing_original','writing_ocr_binding':frozen,'voice_input_id':'confirm-voice'}
        c.execute("INSERT INTO agent_objective VALUES(?,?,?,'learning','Confirm written source','[]','diagnose','active',?,?,NULL,NULL)",(objective,CHILD,sid,now,now))
        c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,orchestrator_version,created_at,client_action_id)
            VALUES(?,?,?,?,'confirm_writing_original','',?,'fixture',?,?)""",(turn,CHILD,sid,transcript,json.dumps(action),now,turn))
        c.execute("INSERT INTO agent_plan VALUES(?,?,?,?,?,'fixture','{}','{}','running',?,?)",(plan,objective,CHILD,sid,turn,now,now))
        c.execute("INSERT INTO agent_step VALUES(?,?,0,'writing.confirm_original','1','server','low','{}','[]','running',?,NULL,NULL,?,?)",(step,plan,step,now,now))
        c.commit()
    return turn


def authorize(ocr,turn,key='authorize'):
    writing,service,p=ocr
    return service.authorize_use(writing[1],confirmation_id=p['confirmation_id'],binding_version=p['binding_version'],
        authorizing_turn_id=turn,client_action_id=key)


def original(ocr,permission):
    writing,_,p=ocr
    return writing[4].bind_confirmed_document_response(writing[1],prompt_id=p['prompt_id'],item_version=p['item_version'],
        artifact_id=p['artifact_id'],revision_id=p['revision_id'],confirmation_id=p['confirmation_id'],
        use_authorization_id=permission['use_authorization_id'],client_action_id='original-page')


def test_exact_page_readback_permission_original_answer_and_restart(ocr):
    writing,service,p=ocr
    db,sid,prompts,prompt,_=writing
    started=readback(ocr)
    assert readback(ocr)==started
    ended=readback(ocr,'ended')
    assert readback(ocr,'ended')==ended
    turn=confirm_turn(ocr)
    permission=authorize(ocr,turn)
    assert authorize(ocr,turn)==permission
    artifact=original(ocr,permission)
    assert original(ocr,permission)==artifact
    answer=prompts.answer(sid,prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],answer=TEXT,client_action_id='submit-page')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        result=bind_written_attempt(c,child_id=CHILD,session_id=sid,prompt_id=p['prompt_id'],item_version=p['item_version'],
            attempt_id=answer['attempt']['attempt_id'],original_artifact_id=artifact['artifact_id'])
        assert result['kind']=='confirmed_document'
        assert c.execute("SELECT extracted_text FROM capture_artifact WHERE artifact_id=?",(artifact['artifact_id'],)).fetchone()[0] is None
        c.commit()
    with db.connect() as c:
        c.execute("UPDATE agent_step SET status='completed'")
        c.execute("UPDATE agent_plan SET status='completed'")
        c.execute("UPDATE daily_session SET status='completed',state='completed'")
        c.commit()
        prompt=c.execute('SELECT * FROM diagnostic_prompt').fetchone()
        attempt=c.execute('SELECT * FROM diagnostic_attempt').fetchone()
        assert original_written_response(c,prompt,attempt)==result
    restarted=WritingOcrAuthorizationService(db,runtime_epoch='process-two')
    assert restarted.read(sid,confirmation_id=p['confirmation_id'],binding_version=p['binding_version'])['response_text']==TEXT
    restarted.retract(sid,confirmation_id=p['confirmation_id'],binding_version=p['binding_version'],client_action_id='retract')
    with db.connect() as c,pytest.raises(ValueError,match='WITHDRAWN'):
        original_written_response(c,prompt,attempt)


@pytest.mark.parametrize('fault',['no_end','negative','role_change','source_change','stale_voice','wrong_page','unadmitted','old_turn'])
def test_confirmation_never_borrows_unrelated_voice_or_changed_page(ocr,fault):
    writing,_,p=ocr
    db=writing[0]
    readback(ocr)
    if fault!='no_end':readback(ocr,'ended')
    turn=confirm_turn(ocr,'不是我的书面答案' if fault=='negative' else '是的')
    with db.connect() as c:
        if fault=='role_change':c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.type','submit_diagnostic_answer')")
        elif fault=='source_change':c.execute("UPDATE capture_artifact SET extracted_text='different text' WHERE artifact_id='page'")
        elif fault=='stale_voice':c.execute("UPDATE voice_capture_binding SET question_validation='stale'")
        elif fault=='wrong_page':c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.writing_ocr_binding.artifact_id','other-page')")
        elif fault=='unadmitted':c.execute("UPDATE agent_step SET status='planned'")
        elif fault=='old_turn':c.execute("UPDATE agent_turn SET created_at='2000-01-01T00:00:00+00:00'")
        c.commit()
    with pytest.raises(ValueError):authorize(ocr,turn)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM writing_ocr_event WHERE kind='authorize'").fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0


def test_readback_old_end_and_restart_never_complete_new_generation(ocr):
    writing,service,p=ocr
    readback(ocr)
    restarted=WritingOcrAuthorizationService(writing[0],runtime_epoch='process-two')
    with pytest.raises(ValueError,match='STALE'):
        readback((writing,restarted,p),'ended')
    readback(ocr,'started','read-two','start-two')
    with pytest.raises(ValueError,match='STALE'):readback(ocr,'ended','read-one','old-end')
    readback(ocr,'ended','read-two','end-two')


def test_confirmation_binding_cannot_move_between_children_or_origins(ocr):
    writing,service,p=ocr
    with pytest.raises(ValueError):service.read('other-session',confirmation_id=p['confirmation_id'],binding_version=p['binding_version'])
    with writing[0].connect() as c:
        c.execute("UPDATE capture_artifact SET child_id='child-junior-3' WHERE artifact_id='page'")
        c.commit()
    with pytest.raises(ValueError):service.read(writing[1],confirmation_id=p['confirmation_id'],binding_version=p['binding_version'])


def test_restart_requires_fresh_readback_before_new_authorization(ocr):
    writing,_,p=ocr
    readback(ocr)
    readback(ocr,'ended')
    turn=confirm_turn(ocr)
    restarted=WritingOcrAuthorizationService(writing[0],runtime_epoch='new-process')
    with pytest.raises(ValueError,match='READBACK_RUNTIME_CHANGED'):
        authorize((writing,restarted,p),turn)


def test_restore_epoch_reset_invalidates_transport_not_existing_authority(ocr):
    writing,service,p=ocr
    readback(ocr)
    readback(ocr,'ended')
    turn=confirm_turn(ocr)
    previous=service.runtime_epoch
    assert service.reset_playback_epoch()!=previous
    with pytest.raises(ValueError,match='READBACK_RUNTIME_CHANGED'):authorize(ocr,turn)
    readback(ocr,'started','new','fresh-start')
    readback(ocr,'ended','new','fresh-end')
    # Existing confirmation voice is now before the fresh readback: cannot reuse.
    with pytest.raises(ValueError,match='PRECEDES_READBACK'):authorize(ocr,turn)


def test_restore_keeps_previously_authorized_source_not_automatic_playback(ocr):
    from home_study_agent.writing_ocr_authorization import resolve_written_document
    writing,service,p=ocr
    readback(ocr)
    readback(ocr,'ended')
    permission=authorize(ocr,confirm_turn(ocr))
    service.reset_playback_epoch()
    with writing[0].connect() as c:
        before=c.total_changes
        result=resolve_written_document(c,child_id=CHILD,session_id=writing[1],prompt_id=p['prompt_id'],
            item_version=p['item_version'],artifact_id=p['artifact_id'],revision_id=p['revision_id'],
            confirmation_id=p['confirmation_id'],use_authorization_id=permission['use_authorization_id'])
        assert result['text']==TEXT and c.total_changes==before


def test_natural_explicit_confirmation_is_not_restricted_to_yes(ocr):
    readback(ocr)
    readback(ocr,'ended')
    assert authorize(ocr,confirm_turn(ocr,'对，是我写的，识别没有错'))['use_authorization_id']


@pytest.mark.parametrize('fault',['capture_sha','voice_changed','voice_withdrawn','stale_capture','page_changed','readback_changed','permission_withdrawn'])
def test_historical_permission_rechecks_exact_sources_not_current_waiting_label(ocr,fault):
    writing,_,p=ocr
    readback(ocr)
    readback(ocr,'ended')
    permission=authorize(ocr,confirm_turn(ocr))
    wrapper=original(ocr,permission)
    scored=writing[2].answer(writing[1],prompt_id=p['prompt_id'],item_version=p['item_version'],answer=TEXT,client_action_id='answer')
    with writing[0].connect() as c:
        c.execute('BEGIN IMMEDIATE')
        bind_written_attempt(c,child_id=CHILD,session_id=writing[1],prompt_id=p['prompt_id'],item_version=p['item_version'],
            attempt_id=scored['attempt']['attempt_id'],original_artifact_id=wrapper['artifact_id'])
        c.commit()
        prompt=c.execute('SELECT * FROM diagnostic_prompt').fetchone()
        attempt=c.execute('SELECT * FROM diagnostic_attempt').fetchone()
        assert prompt['status']=='answered'
        assert original_written_response(c,prompt,attempt)['kind']=='confirmed_document'
        if fault=='capture_sha': c.execute("UPDATE voice_capture_binding SET wav_sha256=?",('0'*64,))
        elif fault=='voice_changed': c.execute("UPDATE voice_input SET transcript='不是我的答案'")
        elif fault=='voice_withdrawn': c.execute("UPDATE voice_capture_binding SET status='cancelled'")
        elif fault=='stale_capture': c.execute("UPDATE voice_capture_binding SET question_validation='stale'")
        elif fault=='page_changed': c.execute("UPDATE capture_artifact SET status='withdrawn' WHERE artifact_id='page'")
        elif fault=='readback_changed': c.execute("UPDATE writing_ocr_event SET payload_json=json_set(payload_json,'$.playback_id','other') WHERE kind='ended'")
        else:
            c.execute("INSERT INTO writing_ocr_event VALUES('withdraw',?,?,'retract','{}','{}','2026-09-08T00:00:00+00:00','withdraw')",(p['confirmation_id'],CHILD))
        with pytest.raises(ValueError): original_written_response(c,prompt,attempt)
        c.rollback()


def test_model_boolean_cannot_replace_confirming_original_voice(ocr):
    readback(ocr)
    readback(ocr,'ended')
    turn=confirm_turn(ocr,'这不是我写的')
    with ocr[0][0].connect() as c:
        c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.confirmed',json('true'),'$.decision','confirm')")
        c.commit()
    with pytest.raises(ValueError,match='EXPLICIT_USE'): authorize(ocr,turn)


def semantic_turn(ocr,transcript='上面读出来的内容和我自己写的完全一样可以作为我的作答'):
    import hashlib
    from home_study_agent.voice_control_intent import VoiceControlIntent
    db,sid,_,_,_=ocr[0]
    VoiceControlIntent(db,None,None,None).initialize()
    turn=confirm_turn(ocr,transcript)
    with db.connect() as c:
        action=json.loads(c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?',(turn,)).fetchone()[0])
        decision=VoiceControlIntent.parse(json.dumps(dict(role='control',control='writing.confirm_original',quote=transcript,confidence=.95)),transcript)
        fingerprint=hashlib.sha256(json.dumps([CHILD,sid,transcript],ensure_ascii=False).encode()).hexdigest()
        now=datetime.now(timezone.utc).isoformat()
        c.execute("INSERT INTO voice_control_intent_request VALUES('semantic',?,?,?,?, 'completed',?,?,?,NULL)",
            (CHILD,sid,fingerprint,json.dumps({'writing_ocr':action['writing_ocr_binding']}),json.dumps(decision),now,now))
        action['writing_ocr_semantic_receipt']={'request_key':'semantic'}
        c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?',(json.dumps(action),turn))
        c.commit()
    return turn


def test_verified_semantic_permission_survives_execution_receipt_but_not_decision_revision(ocr):
    readback(ocr)
    readback(ocr,'ended')
    permission=authorize(ocr,semantic_turn(ocr))
    assert original(ocr,permission)['artifact_id']
    with ocr[0][0].connect() as c:
        c.execute("UPDATE voice_control_intent_request SET execution_receipt_json='{}'")
        c.commit()
    assert original(ocr,permission)['artifact_id']
    with ocr[0][0].connect() as c:
        c.execute("UPDATE voice_control_intent_request SET decision_json='{\"status\":\"non_control\"}'")
        c.commit()
    # Read the original authority again, not the idempotent historical wrapper.
    from home_study_agent.writing_ocr_authorization import resolve_written_document
    p=ocr[2]
    with ocr[0][0].connect() as c,pytest.raises(ValueError,match='SEMANTIC'):
        resolve_written_document(c,child_id=CHILD,session_id=ocr[0][1],prompt_id=p['prompt_id'],item_version=p['item_version'],
            artifact_id=p['artifact_id'],revision_id=p['revision_id'],confirmation_id=p['confirmation_id'],
            use_authorization_id=permission['use_authorization_id'])


@pytest.mark.parametrize('fault',['fingerprint','owner','target','pending','decision','negative'])
def test_semantic_flag_is_not_authority_without_exact_completed_receipt(ocr,fault):
    readback(ocr)
    readback(ocr,'ended')
    turn=semantic_turn(ocr,'不是我写的需要修改' if fault=='negative' else '上面读出来的内容和我自己写的完全一样可以作为我的作答')
    with ocr[0][0].connect() as c:
        if fault=='fingerprint':c.execute("UPDATE voice_control_intent_request SET fingerprint='wrong'")
        elif fault=='owner':c.execute("UPDATE voice_control_intent_request SET child_id='child-junior-3'")
        elif fault=='target':c.execute("UPDATE voice_control_intent_request SET binding_json='{}'")
        elif fault=='pending':c.execute("UPDATE voice_control_intent_request SET status='pending'")
        elif fault=='decision':c.execute("UPDATE voice_control_intent_request SET decision_json='{\"status\":\"control\",\"control\":\"writing.read_original\"}'")
        c.commit()
    with pytest.raises(ValueError):authorize(ocr,turn)
