"""An actual checked Tutor reply is local evidence, never another original grade."""
import json

import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from test_reviewed_tutor_projection_http import prepared_client
from test_tutor_first_check_http import OverhelpGateway, PREFIX


def lesson(tmp_path,*,scored=False):
    gateway = OverhelpGateway()
    client, db, settings, *_, sid, payload = prepared_client(tmp_path, gateway)
    if scored:
        states=['check_in','classroom_recall','homework_capture','plan_review','study']
        for i,(previous,next_state) in enumerate(zip(states,states[1:])):
            if previous=='classroom_recall':
                assert client.post(f'/v1/sessions/{sid}/recalls/none',json={'client_action_id':'check-no-classroom-recall'}).status_code==200
            if previous=='homework_capture':
                assert client.post(f'/v1/sessions/{sid}/homework/none',json={'client_action_id':'check-no-homework'}).status_code==200
            moved=client.post(f'/v1/sessions/{sid}/transition',json={'expected_state':previous,'target_state':next_state,'reason':'synthetic checked lesson','client_action_id':f'check-study-{i}'})
            assert moved.status_code==200,moved.text
        answer=client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer',json={**{k:payload[k] for k in ('prompt_id','item_version')},'answer':'1,2','client_action_id':'original-before-help'})
        assert answer.status_code==200,answer.text
    turn = client.post(f'/v1/sessions/{sid}/tutor', json=payload).json()
    assert turn['answer'] == PREFIX
    base = f'/v1/sessions/{sid}/tutor-playback'
    view = client.post(base+'/begin', json={'turn_id':turn['turn_id'],'client_action_id':'check-begin'}).json()['playback']
    def operation(path, **values):
        nonlocal view
        binding = {k:view[k] for k in ('playback_id','source_version','generation')}
        binding['expected_revision']=view['revision']
        response=client.post(base+path,json={**binding,**values})
        assert response.status_code==200,response.text
        view=response.json()['playback']
    operation('/control',command='resume',client_action_id='check-resume')
    for i in range(len(view['presentation']['segments'])):
        segment_id=view['presentation']['segments'][view['segment_index']]['segment_id']
        operation('/segment',segment_id=segment_id,event='started',client_action_id=f'check-start-{i}')
        operation('/segment',segment_id=segment_id,event='ended',client_action_id=f'check-end-{i}')
    assert view['status']=='playback_finished'
    return client,db,settings,gateway,sid,turn,view


def test_completed_check_response_is_bound_saved_replayed_and_restarted(tmp_path):
    client,db,settings,gateway,sid,turn,view=lesson(tmp_path)
    try:
        endpoint=f'/v1/sessions/{sid}/tutor-check'
        pending=client.get(endpoint)
        assert pending.status_code==200,pending.text
        check=pending.json()
        assert check['status']=='waiting_response' and check['question']==PREFIX
        assert check['binding']=={'turn_id':turn['turn_id'],'source_version':view['source_version']}
        answer={'binding':check['binding'],'response_text':'我还记得要先找出三个系数。','client_action_id':'check-answer-public'}
        response=client.post(endpoint+'/responses',json=answer)
        assert response.status_code==200,response.text
        saved=response.json()
        assert saved['status']=='response_saved' and saved['mastery_update_allowed'] is False
        assert saved['response']['response_text']==answer['response_text']
        assert saved['response']['evidence_basis']=='tutor_check_response'
        assert saved['response']['confidence']==0
        assert client.post(endpoint+'/responses',json=answer).json()['response']==saved['response']
        assert client.get(endpoint).json()['status']=='response_saved'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_feedback').fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
        assert len(gateway.requests)==1
        client.__exit__(None,None,None);client=None
        with TestClient(create_app(settings,model_gateway=gateway),client=('127.0.0.1',41200),headers={'X-HSC-Session':settings.session_token}) as restarted:
            assert restarted.get(endpoint).json()['response']==saved['response']
            assert restarted.post(endpoint+'/responses',json=answer).json()['response']==saved['response']
    finally:
        if client:client.__exit__(None,None,None)


@pytest.mark.parametrize('mutation',['wrong_session','source_changed','newer_turn','changed_response','changed_binding'])
def test_late_changed_or_foreign_check_never_receives_a_new_answer(tmp_path,mutation):
    client,db,_,_,sid,turn,view=lesson(tmp_path)
    try:
        endpoint=f'/v1/sessions/{sid}/tutor-check'
        pending=client.get(endpoint)
        assert pending.status_code==200,pending.text
        answer={'binding':pending.json()['binding'],'response_text':'先写标准形式。','client_action_id':'check-rejected-public'}
        if mutation in {'changed_response','changed_binding'}:
            assert client.post(endpoint+'/responses',json=answer).status_code==200
            if mutation=='changed_response':answer['response_text']='这是另一次回答。'
            else:answer['binding']['source_version']='tutor_source_v1:changed'
        elif mutation=='wrong_session':
            other='foreign-check-session'
            with db.connect() as c:
                original=c.execute('SELECT * FROM daily_session WHERE session_id=?',(sid,)).fetchone()
                c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?",(sid,))
                cols=list(original.keys());values=[original[k] for k in cols]
                values[cols.index('session_id')]=other;values[cols.index('child_id')]='child-primary-4';values[cols.index('client_action_id')]='foreign-check-action'
                c.execute('INSERT INTO daily_session ('+','.join(cols)+') VALUES ('+','.join('?' for _ in cols)+')',values);c.commit()
            endpoint=f'/v1/sessions/{other}/tutor-check'
        elif mutation=='source_changed':
            with db.connect() as c:c.execute("UPDATE tutor_turn SET answer=answer||' changed' WHERE turn_id=?",(turn['turn_id'],));c.commit()
        else:
            with db.connect() as c:
                original=c.execute('SELECT * FROM tutor_turn WHERE turn_id=?',(turn['turn_id'],)).fetchone()
                cols=list(original.keys());values=[original[k] for k in cols];values[cols.index('turn_id')]='newer-owned-turn';values[cols.index('client_action_id')]='newer-owned-action'
                c.execute('INSERT INTO tutor_turn ('+','.join(cols)+') VALUES ('+','.join('?' for _ in cols)+')',values);c.commit()
        rejected=client.post(endpoint+'/responses',json=answer)
        assert rejected.status_code==409,rejected.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_feedback').fetchone()[0]==(1 if mutation.startswith('changed_') else 0)
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
    finally:client.__exit__(None,None,None)


@pytest.mark.parametrize('change',['withdraw','consent_missing','paused','changed_presentation','blank'])
def test_current_authority_and_actual_presentation_are_rechecked_before_save(tmp_path,change):
    client,db,_,gateway,sid,turn,view=lesson(tmp_path)
    try:
        endpoint=f'/v1/sessions/{sid}/tutor-check'
        original=client.get(endpoint).json()
        assert original['status']=='waiting_response'
        with db.connect() as c:
            if change=='withdraw':c.execute("UPDATE curriculum_first_source SET state='withdrawn'")
            elif change=='consent_missing':c.execute("DELETE FROM consent_record WHERE scope='cloud_model_text_allowed'")
            elif change=='paused':c.execute("UPDATE tutor_playback_checkpoint SET status='paused' WHERE playback_id=?",(view['playback_id'],))
            elif change=='changed_presentation':
                board=json.loads(c.execute('SELECT presentation_json FROM tutor_playback_checkpoint WHERE playback_id=?',(view['playback_id'],)).fetchone()[0])
                board['segments'][-1]['narration']='这是另一道检查问题吗？'
                c.execute('UPDATE tutor_playback_checkpoint SET presentation_json=? WHERE playback_id=?',(json.dumps(board),view['playback_id']))
            c.commit()
        response=client.post(endpoint+'/responses',json={'binding':original['binding'],'response_text':'  ' if change=='blank' else '先回忆公式。','client_action_id':'check-authority-response'})
        assert response.status_code==409,response.text
        if change!='blank':assert client.get(endpoint).json()['status']=='unavailable'
        assert len(gateway.requests)==1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_feedback').fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
    finally:client.__exit__(None,None,None)


def test_actual_voice_answer_role_executes_check_capture_without_original_grading(tmp_path,monkeypatch):
    from test_content_free_voice_control_http import ControlGateway
    from test_voice_runtime_binding import capture,interpret
    client,db,settings,_,sid,turn,view=lesson(tmp_path,scored=True)
    client.__exit__(None,None,None)
    text='我先把方程写成标准形式，再找三个系数。'
    gateway=ControlGateway(text,None,role='answer')
    with TestClient(create_app(settings,model_gateway=gateway),client=('127.0.0.1',41200),headers={'X-HSC-Session':settings.session_token}) as client:
        target=turn['output_envelope']['local_teaching_control']['question_target']['binding']
        reference={'kind':'diagnostic_help','binding':{k:target[k] for k in ('prompt_id','item_version')}}
        voice=capture(client,settings,monkeypatch,sid,text,reference,key='actual-check-capture',child='child-junior-3')
        heard=interpret(client,sid,text,'actual-check-reply',voice)
        with db.connect() as c:
            debug=[dict(x) for x in c.execute('SELECT status,decision_json,binding_json FROM voice_control_intent_request')]
            captured=[dict(x) for x in c.execute('SELECT created_at,speech_started_at,question_reference_json,question_validation FROM voice_capture_binding')]
            ended=[dict(x) for x in c.execute('SELECT status,updated_at FROM tutor_playback_checkpoint')]
        assert heard['action']['type']=='submit_tutor_check_response',json.dumps({'intent':heard['intent'],'action':heard['action']['type'],'journal':debug,'captured':captured,'ended':ended},ensure_ascii=False)
        assert heard['runtime_execution_location']=='server'
        executed=client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert executed.status_code==200,executed.text
        receipt=executed.json()['execution']['result']
        assert receipt['status']=='response_saved' and receipt['response']['response_text']==text
        assert receipt['mastery_update_allowed'] is False and receipt['independent_evidence'] is False
        assert client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute").json()['execution']['result']['response']==receipt['response']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_feedback').fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1


@pytest.mark.parametrize('change',['role','frozen_voice','withdraw','response_text'])
def test_delayed_runtime_write_rechecks_role_voice_source_and_original_text(tmp_path,monkeypatch,change):
    from test_content_free_voice_control_http import ControlGateway
    from test_voice_runtime_binding import capture,interpret
    client,db,settings,_,sid,turn,_=lesson(tmp_path,scored=True)
    client.__exit__(None,None,None)
    text='我先写出标准形式，再找系数。'
    with TestClient(create_app(settings,model_gateway=ControlGateway(text,None,role='answer')),client=('127.0.0.1',41200),headers={'X-HSC-Session':settings.session_token}) as client:
        target=turn['output_envelope']['local_teaching_control']['question_target']['binding']
        voice=capture(client,settings,monkeypatch,sid,text,{'kind':'diagnostic_help','binding':{k:target[k] for k in ('prompt_id','item_version')}},key='delayed-check-capture',child='child-junior-3')
        heard=interpret(client,sid,text,'delayed-check-reply',voice)
        assert heard['action']['type']=='submit_tutor_check_response'
        with db.connect() as c:
            if change=='role':
                decision=json.loads(c.execute('SELECT decision_json FROM voice_control_intent_request').fetchone()[0]);decision['role']='question'
                c.execute('UPDATE voice_control_intent_request SET decision_json=?',(json.dumps(decision),))
            elif change=='frozen_voice':
                binding=json.loads(c.execute('SELECT binding_json FROM voice_control_intent_request').fetchone()[0]);binding['tutor_check']['voice_input_id']='other-voice'
                c.execute('UPDATE voice_control_intent_request SET binding_json=?',(json.dumps(binding),))
            elif change=='withdraw':c.execute("UPDATE curriculum_first_source SET state='withdrawn'")
            else:
                row=c.execute('SELECT step_id,arguments_json FROM agent_step WHERE capability_name=?',('tutor.capture_check_response',)).fetchone()
                args=json.loads(row['arguments_json']);args['response']='不是那次原话'
                c.execute('UPDATE agent_step SET arguments_json=? WHERE step_id=?',(json.dumps(args),row['step_id']))
            c.commit()
        rejected=client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert rejected.status_code==409,rejected.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_feedback').fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1


def test_audio_admitted_before_the_displayed_check_cannot_answer_that_later_check(tmp_path,monkeypatch):
    from test_content_free_voice_control_http import ControlGateway
    from test_voice_runtime_binding import capture,interpret
    client,db,settings,_,sid,turn,_=lesson(tmp_path,scored=True)
    client.__exit__(None,None,None)
    text='我先找出三个系数。'
    with TestClient(create_app(settings,model_gateway=ControlGateway(text,None,role='answer')),client=('127.0.0.1',41200),headers={'X-HSC-Session':settings.session_token}) as client:
        target=turn['output_envelope']['local_teaching_control']['question_target']['binding']
        voice=capture(client,settings,monkeypatch,sid,text,{'kind':'diagnostic_help','binding':{k:target[k] for k in ('prompt_id','item_version')}},key='early-check-capture',child='child-junior-3')
        with db.connect() as c:
            c.execute("UPDATE voice_capture_binding SET created_at='2020-01-01T00:00:00+00:00' WHERE voice_input_id=?",(voice,));c.commit()
        heard=interpret(client,sid,text,'early-check-reply',voice)
        assert heard['action']['type']!='submit_tutor_check_response'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_feedback').fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
