"""Real HTTP/SQLite ownership; fake local decoder/cloud output, never acoustic proof."""
import json
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.local_media_ai import LocalMediaAI, ASR_MODEL_NAME
from home_study_agent.repository import StudyRepository, utc_now
from test_onboarding_local_response_http import route, payload
from test_diagnostic_prompt_http import environment, start, CHILD, OTHER, prepare, binding, execute
from test_learning_runtime_http import NoCloud
import pytest


def ordinary(settings,db,sid,key='ordinary-response',**marker):
    with db.connect() as c:state=c.execute('SELECT state FROM daily_session WHERE session_id=?',(sid,)).fetchone()[0]
    body=payload(settings,key);body['purpose']='agent_command';body['ordinary_response_binding']={
        'version':'ordinary_prompt_response_v1','session_state':state,**marker};return body


def test_real_ordinary_plain_marker_saved_and_replayed(environment,route):
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);body=ordinary(settings,db,sid)
        response=client.post(f'/v1/sessions/{sid}/asr',json=body)
        assert response.status_code==200,response.text
        value=response.json();assert value['local_only'] is True and value['cloud_used'] is False
        assert value['local_path'] is None and value['model_version']==ASR_MODEL_NAME
        assert value['capture_binding']['ordinary_response_binding']==body['ordinary_response_binding']
        assert client.post(f'/v1/sessions/{sid}/asr',json=body).json()['voice_input_id']==value['voice_input_id']
    assert route=={'local':1,'cloud':0}
    with db.connect() as c:
        assert json.loads(c.execute('SELECT ordinary_response_binding_json FROM voice_capture_binding').fetchone()[0])==body['ordinary_response_binding']
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0]==0


def count_voice(db):
    with db.connect() as c:return c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0]

def agent(db,sid,key='source-agent',child=CHILD,status='completed'):
    execute(db,'''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,
        orchestrator_version,tool_status,created_at,client_action_id) VALUES(?,?,?,?,?,?,?,'synthetic',?,'2026-10-10T02:00:00Z',?)''',
        (key,child,sid,'synthetic','none','synthetic reply','{}',status,key));return key

def tutor(db,sid,key='source-tutor',board=None):
    from home_study_agent.repository import StudyRepository
    result={'answer':'Synthetic explanation'}
    if board is not None:result['blackboard']=board
    return StudyRepository(db).save_tutor_turn(sid,None,'math','Synthetic question','Synthetic explanation','test','test',key,
         output_envelope={'result':result})['turn_id']

def reserve(client,sid,tid,key='reserve-voice'):
    r=client.post(f'/v1/sessions/{sid}/tutor-voice-playback/reserve',json={'turn_id':tid,'client_action_id':key})
    assert r.status_code==200,r.text
    return r.json()

def event(client,sid,row,name,key=None,**changes):
    body={k:row[k] for k in ('playback_id','source_version','generation')}
    body.update(lease_id='actual-owned-lease',lease_generation=1,lease_kind='local_sapi',client_action_id=key or ('playback-'+name))
    body.update(changes)
    return client.post(f'/v1/sessions/{sid}/tutor-voice-playback/{name}',json=body)

def finished(client,sid,tid):
    r=reserve(client,sid,tid)
    assert event(client,sid,r,'start').status_code==200
    assert event(client,sid,r,'natural-end').status_code==200
    return r

@pytest.mark.parametrize('change',[
    {'local_only':False},{'local_only':1},{'local_only':'true'},
    {'purpose':'wake_word_setup'},{'capture_reference':None},
    {'ordinary_response_binding':{'version':'ordinary_prompt_response_v1','session_state':'check_in','unknown':True}},
    {'ordinary_response_binding':{'version':'ordinary_prompt_response_v1','session_state':True}},
    {'ordinary_response_binding':{'version':'ordinary_prompt_response_v1','session_state':'check_in','source_tutor_voice_playback_id':'unknown'}},
    {'capture_reference':{'child_id':CHILD,'local_recovery_only':True}},
    {'capture_reference':{'child_id':CHILD,'pending_evaluation_turn_id':'pending-not-real'}},
])
def test_marker_requires_strict_paired_contract(environment,route,change):
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);body=ordinary(settings,db,sid);body.update(change)
        assert client.post(f'/v1/sessions/{sid}/asr',json=body).status_code in {409,422}
        assert client.post('/v1/standby/asr',json=ordinary(settings,db,sid)).status_code==409
    assert count_voice(db)==0 and route=={'local':0,'cloud':0}

@pytest.mark.parametrize('kind',['state','agent','other-child','pending-agent'])
def test_current_owner_or_latest_completed_agent_required(environment,route,kind):
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client)
        aid=agent(db,sid,child=OTHER if kind=='other-child' else CHILD,status='pending' if kind=='pending-agent' else 'completed')
        body=ordinary(settings,db,sid,source_agent_turn_id=aid)
        if kind=='state':execute(db,"UPDATE daily_session SET state='reflection' WHERE session_id=?",(sid,))
        if kind=='agent':agent(db,sid,'later-agent')
        assert client.post(f'/v1/sessions/{sid}/asr',json=body).status_code==409
    assert count_voice(db)==0 and route=={'local':0,'cloud':0}

def test_actual_agent_and_question_reference_preserved(environment,route):
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);prompt=prepare(client,sid)['prompt'];aid=agent(db,sid)
        body=ordinary(settings,db,sid,source_agent_turn_id=aid);body['capture_reference']['question_reference']=binding(prompt)
        r=client.post(f'/v1/sessions/{sid}/asr',json=body);assert r.status_code==200,r.text
        assert r.json()['capture_binding']['question_reference']['binding']==binding(prompt)
        # A later source makes a replay a rejection, never an upgraded authority.
        agent(db,sid,'new-agent')
        assert client.post(f'/v1/sessions/{sid}/asr',json=body).status_code==409
    assert route=={'local':1,'cloud':0}

@pytest.mark.parametrize('kind',['state','source','owner','cancel','tutor-generation'])
def test_decode_late_change_cannot_publish(environment,route,monkeypatch,kind):
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);aid=agent(db,sid);body=ordinary(settings,db,sid,source_agent_turn_id=aid)
        tid=None
        if kind=='tutor-generation':
            tid=tutor(db,sid);receipt=finished(client,sid,tid)
            body['ordinary_response_binding'].update(source_tutor_turn_id=tid,source_tutor_voice_playback_id=receipt['playback_id'])
        original=LocalMediaAI._recognize_locally
        def decode(*args):
            text=original(*args)
            if kind=='state':execute(db,"UPDATE daily_session SET state='reflection' WHERE session_id=?",(sid,))
            if kind=='source':agent(db,sid,'new-source-agent')
            if kind=='owner':execute(db,'UPDATE daily_session SET child_id=? WHERE session_id=?',(OTHER,sid))
            if kind=='cancel':execute(db,"UPDATE voice_capture_binding SET status='cancelled' WHERE client_action_id=?",(body['client_action_id'],))
            if kind=='tutor-generation':reserve(client,sid,tid,'reserve-new-during-decode')
            return text
        monkeypatch.setattr(LocalMediaAI,'_recognize_locally',decode)
        assert client.post(f'/v1/sessions/{sid}/asr',json=body).status_code in {400,409,422}
    assert count_voice(db)==0 and route=={'local':1,'cloud':0}

@pytest.mark.parametrize('cloud,model',[(True,ASR_MODEL_NAME),(False,'unknown-model')])
def test_bad_local_decoder_provenance_rejected(environment,route,monkeypatch,cloud,model):
    settings,db=environment
    monkeypatch.setattr(LocalMediaAI,'_recognize_wav',lambda *a,**kw:('synthetic',500,model,cloud))
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);assert client.post(f'/v1/sessions/{sid}/asr',json=ordinary(settings,db,sid)).status_code==409
    assert count_voice(db)==0

def test_no_board_natural_receipt_durable_local_capture(environment,route):
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);tid=tutor(db,sid);receipt=reserve(client,sid,tid)
        body=ordinary(settings,db,sid,source_tutor_turn_id=tid,source_tutor_voice_playback_id=receipt['playback_id'])
        assert client.post(f'/v1/sessions/{sid}/asr',json=body).status_code==409
        assert event(client,sid,receipt,'natural-end').status_code==409
        started=event(client,sid,receipt,'start');assert started.status_code==200
        ended=event(client,sid,receipt,'natural-end');assert ended.status_code==200
        assert event(client,sid,receipt,'natural-end').json()==ended.json()
    # Actual backend reconstruction keeps only durable metadata, not FE grants.
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        same=reserve(client,sid,tid);assert same['status']=='finished' and same['playback_id']==receipt['playback_id']
        r=client.post(f'/v1/sessions/{sid}/asr',json=body);assert r.status_code==200,r.text
        assert r.json()['capture_binding']['ordinary_response_binding']==body['ordinary_response_binding']
    with db.connect() as c:
        row=c.execute('SELECT * FROM tutor_voice_playback').fetchone()
        assert row['event_source']=='renderer_natural_end_callback_v1'
        assert c.execute('SELECT COUNT(*) FROM learning_evidence').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0]==0
    assert route=={'local':1,'cloud':0}

@pytest.mark.parametrize('kind',['generation','cancel','wrong-lease','new-turn','mutated-answer','cross-session','extra','bad-bool'])
def test_natural_receipt_rejects_old_foreign_or_changed_source(environment,route,kind):
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);tid=tutor(db,sid);row=reserve(client,sid,tid);assert event(client,sid,row,'start').status_code==200
        changes={};other_sid=sid
        if kind=='generation':
            newer=reserve(client,sid,tid,'new-reservation');assert newer['generation']==row['generation']+1
            assert event(client,sid,newer,'cancel',lease_id=None,lease_generation=None,lease_kind=None).status_code==200
            assert reserve(client,sid,tid)['status']=='cancelled'
        if kind=='cancel':assert event(client,sid,row,'cancel').status_code==200
        if kind=='wrong-lease':changes['lease_id']='foreign-lease'
        if kind=='new-turn':tutor(db,sid,'new-turn')
        if kind=='mutated-answer':execute(db,"UPDATE tutor_turn SET answer='changed' WHERE turn_id=?",(tid,))
        if kind=='cross-session':
            execute(db,"UPDATE daily_session SET status='completed' WHERE session_id=?",(sid,))
            other_sid=start(client,OTHER,'other-session')
        if kind=='extra':changes['natural']=True
        if kind=='bad-bool':changes['lease_generation']=True
        assert event(client,other_sid,row,'natural-end',**changes).status_code in {409,422}
    assert count_voice(db)==0 and route=={'local':0,'cloud':0}

def test_board_cannot_bypass_persistent_final_checkpoint(environment,route):
    from home_study_agent.tutor_blackboard import parse_tutor_blackboard_response
    from home_study_agent.tutor_playback import TutorPlaybackService
    settings,db=environment
    _,board=parse_tutor_blackboard_response(json.dumps({'blackboard':{'title':'Synthetic','segments':[
         {'narration':'Synthetic explanation','blocks':[{'kind':'text','text':'One'}]}]}}),'math',1)
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);tid=tutor(db,sid,board=board)
        assert client.post(f'/v1/sessions/{sid}/tutor-voice-playback/reserve',json={'turn_id':tid,'client_action_id':'cannot-no-board'}).status_code==409
        body=ordinary(settings,db,sid,source_tutor_turn_id=tid)
        assert client.post(f'/v1/sessions/{sid}/asr',json=body).status_code==409
        service=TutorPlaybackService(db);view=service.begin(sid,turn_id=tid,client_action_id='board-start')
        args=lambda v:dict(playback_id=v['playback_id'],source_version=v['source_version'],generation=v['generation'],expected_revision=v['revision'])
        view=service.control(sid,**args(view),command='resume',client_action_id='board-resume')
        view=service.record_segment(sid,**args(view),segment_id=view['presentation']['segments'][0]['segment_id'],event='started',client_action_id='segment-start')
        view=service.record_segment(sid,**args(view),segment_id=view['presentation']['segments'][0]['segment_id'],event='ended',client_action_id='segment-end')
        assert view['status']=='playback_finished'
        r=client.post(f'/v1/sessions/{sid}/asr',json=body);assert r.status_code==200,r.text
    assert route=={'local':1,'cloud':0}

def test_concurrent_reservations_monotonic_and_terminal_fenced(environment,route):
    from concurrent.futures import ThreadPoolExecutor
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);tid=tutor(db,sid)
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows=list(pool.map(lambda key:reserve(client,sid,tid,key),['reserve-concurrent-a','reserve-concurrent-b']))
        old,new=sorted(rows,key=lambda row:row['generation']);assert [old['generation'],new['generation']]==[1,2]
        assert event(client,sid,old,'start').status_code==409
        assert event(client,sid,new,'start').status_code==200
        # Concurrent cancel/end serialize; terminal cancellation never revives.
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda op:event(client,sid,new,op),['cancel','natural-end']))
        assert {r.status_code for r in results}<={200,409}
        assert reserve(client,sid,tid,'reserve-concurrent-b' if rows[1]['generation']==2 else 'reserve-concurrent-a')['status']=='cancelled'
        assert event(client,sid,new,'natural-end').status_code==409
    assert count_voice(db)==0

def test_old_capture_cannot_acquire_marker_or_change_it(environment,route):
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);legacy=payload(settings,'old-plain-response')
        assert client.post(f'/v1/sessions/{sid}/asr',json=legacy).status_code==200
        upgraded=ordinary(settings,db,sid,key='old-plain-response')
        assert client.post(f'/v1/sessions/{sid}/asr',json=upgraded).status_code==409
        body=ordinary(settings,db,sid,key='new-ordinary-response')
        assert client.post(f'/v1/sessions/{sid}/asr',json=body).status_code==200
        changed=dict(body);changed['ordinary_response_binding']=dict(body['ordinary_response_binding'],source_agent_turn_id=agent(db,sid))
        assert client.post(f'/v1/sessions/{sid}/asr',json=changed).status_code==409
    assert route=={'local':2,'cloud':0}

@pytest.mark.parametrize('board',[{}, {'segments':[]}])
def test_actual_invalid_board_cannot_be_reclassified_as_no_board(environment,route,board):
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);tid=tutor(db,sid,board=board)
        response=client.post(f'/v1/sessions/{sid}/tutor-voice-playback/reserve',json={'turn_id':tid,'client_action_id':'invalid-board-source'})
        assert response.status_code==409
    assert count_voice(db)==0


def test_cancelled_finished_receipt_and_replay_no_timestamp_rewrite(environment,route):
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);tid=tutor(db,sid);row=finished(client,sid,tid)
        with db.connect() as c:before=dict(c.execute('SELECT * FROM tutor_voice_playback').fetchone())
        assert event(client,sid,row,'natural-end').status_code==200
        with db.connect() as c:assert dict(c.execute('SELECT * FROM tutor_voice_playback').fetchone())==before
        assert event(client,sid,row,'cancel').status_code==200
        assert event(client,sid,row,'natural-end').status_code==409
        body=ordinary(settings,db,sid,source_tutor_turn_id=tid,source_tutor_voice_playback_id=row['playback_id'])
        assert client.post(f'/v1/sessions/{sid}/asr',json=body).status_code==409
    assert route=={'local':0,'cloud':0}

def test_actual_pending_evaluation_cannot_omit_reference_to_borrow_lane(environment,route):
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);aid=agent(db,sid)
        execute(db,'''INSERT INTO agent_plan(plan_id,child_id,session_id,agent_turn_id,planner_version,context_manifest_json,
            plan_json,status,created_at,updated_at) VALUES('pending-plan',?,?,?,'synthetic','{}','{}','running',?,?)''',
            (CHILD,sid,aid,utc_now(),utc_now()))
        execute(db,'''INSERT INTO agent_step(step_id,plan_id,step_index,capability_name,capability_version,execution_location,
            risk_level,arguments_json,expected_evidence_json,status,idempotency_key,created_at,updated_at)
            VALUES('pending-step','pending-plan',0,'learning.evaluate_response','test','server','low','{}','{}','running','pending-key',?,?)''',
            (utc_now(),utc_now()))
        assert client.post(f'/v1/sessions/{sid}/asr',json=ordinary(settings,db,sid)).status_code==409
    assert route=={'local':0,'cloud':0} and count_voice(db)==0


def test_local_unavailable_ordinary_never_falls_back_to_configured_cloud(environment,route,monkeypatch):
    from pathlib import Path
    settings,db=environment
    monkeypatch.setattr(LocalMediaAI,'_asr_files',lambda *_:{'missing':Path('/private/tmp/hsc-no-such-public-model')})
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);r=client.post(f'/v1/sessions/{sid}/asr',json=ordinary(settings,db,sid))
        assert r.status_code==503 and r.json()['code']=='ASR_MODEL_NOT_READY'
    assert route=={'local':0,'cloud':0} and count_voice(db)==0
