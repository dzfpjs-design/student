"""Synthetic source/privacy isolation. No network or automatic score claims."""
import json
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.formative_evaluation_projection import build_formative_projection,revalidate_formative_projection
from home_study_agent.learning_rubric import evaluation_source_boundary
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_writing_response_origin import writing,capture,answer,bind,CHILD,TEXT
from test_pet_media_evidence import media


def objective(db,sid,prompt,attempt):
    from home_study_agent.repository import utc_now
    from home_study_agent.learning_rubric import rubric_dimensions
    oid,turn,plan,step = [uuid4().hex for _ in range(4)]
    with db.connect() as c:
        item=c.execute('SELECT di.expected_answer_json,kn.subject FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id) WHERE diagnostic_item_id=?',
            (prompt['diagnostic_item_id'],)).fetchone()
    criteria=json.loads(item['expected_answer_json']).get('task_points') or [d['criterion'] for d in rubric_dimensions(item['subject'])]
    binding = dict(objective_id=oid,child_id=CHILD,session_id=sid,prompt_id=prompt['prompt_id'],
        item_version=prompt['item_version'],diagnostic_item_id=prompt['diagnostic_item_id'],phase='diagnose',
        success_criteria=criteria,created_at=prompt['created_at'],version='diagnostic_objective_binding_v1',
        local_only=True,cloud_transmission_allowed=False)
    now=utc_now()
    with db.connect() as c:
        c.execute("INSERT INTO agent_objective VALUES(?,?,?,'learning','PRIVATE objective summary',?,'diagnose','active',?,?,NULL,NULL)",
            (oid,CHILD,sid,json.dumps(criteria),prompt['created_at'],now))
        c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,
            orchestrator_version,created_at,client_action_id) VALUES(?,?,?,'PRIVATE history','diagnostic.start','',?,'fixture',?,?)""",
            (turn,CHILD,sid,json.dumps({'type':'start_diagnostic','local_only':True,'cloud_transmission_allowed':False}),now,turn))
        c.execute("INSERT INTO agent_plan VALUES(?,?,?,?,?,'fixture','{}',?,'completed',?,?)",(plan,oid,CHILD,sid,turn,
            json.dumps({'local_learning_binding':binding}),now,now))
        c.execute("INSERT INTO agent_step VALUES(?,?,0,'diagnostic.start','1','server','low','{}','[]','completed',?,NULL,NULL,?,?)",(step,plan,step,now,now))
        c.commit()
    return dict(child_id=CHILD,session_id=sid,prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],objective_id=oid)


@pytest.fixture
def projection(writing):
    db,sid,_,p,_=writing
    original=capture(writing)
    scored=answer(writing)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        bind(c,writing,original)
        c.commit()
    PrivacyLifecycleService(db,db.path.parent).initialize()
    return writing,objective(db,sid,p,scored['attempt'])


def test_payload_is_rebuilt_not_stripped_from_local_envelope_and_get_is_readonly(projection):
    writing,scope=projection
    with writing[0].connect() as c:
        before=c.total_changes
        boundary=evaluation_source_boundary(c,CHILD,objective_id=scope['objective_id'])
        assert boundary['cloud_evaluator_blocked']
        result=build_formative_projection(c,**scope)
        assert result['status']=='ready_for_runtime_checks',result
        assert result['payload']['response']==TEXT
        assert set(result['payload'])=={'question','response','rubric'}
        assert set(result['payload']['rubric'])=={'dimensions','task_points'}
        body=json.dumps(result['payload'])
        assert all(secret not in body for secret in ['PRIVATE',CHILD,scope['session_id'],scope['prompt_id'],'local_only','artifact_id'])
        assert result['cloud_transmission_allowed'] is False
        assert not result['automatic_certification']
        assert revalidate_formative_projection(c,projection_version=result['projection_version'],**scope)==result
        assert c.total_changes==before
    with Database(writing[0].path).connect() as c:
        assert build_formative_projection(c,**scope)==result


@pytest.mark.parametrize('fault',['source','text','privacy','other_child','other_prompt','missing_original',
    'voice_not_writing','nested_local','mixed_task','mixed_source','original_change','consent_record','material_ref','late_goal'])
def test_no_permission_laundering_or_current_target_fallback(projection,fault):
    writing,scope=projection
    db=writing[0]
    with db.connect() as c:
        good=build_formative_projection(c,**scope)
        assert good['status']=='ready_for_runtime_checks',good
        c.execute('BEGIN IMMEDIATE')
        if fault=='source': c.execute("UPDATE diagnostic_item SET source_ref='local-document' WHERE diagnostic_item_id='diag-pet-writing-1'")
        elif fault=='text': c.execute("UPDATE diagnostic_item SET prompt='Local copied material' WHERE diagnostic_item_id='diag-pet-writing-1'")
        elif fault=='privacy': c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
        elif fault=='other_child': scope={**scope,'child_id':'child-junior-3'}
        elif fault=='other_prompt': scope={**scope,'prompt_id':'later-prompt'}
        elif fault=='missing_original': c.execute("DELETE FROM capture_artifact WHERE artifact_type='writing_original'")
        elif fault=='voice_not_writing': c.execute("UPDATE capture_artifact SET details_json=json_set(details_json,'$.origin.kind','voice_input')")
        elif fault=='nested_local': c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.material',json('{\"local_only\":true,\"text\":\"private\"}'))")
        elif fault=='mixed_task': c.execute("UPDATE agent_step SET capability_name='document.capture'")
        elif fault=='mixed_source': c.execute("UPDATE agent_step SET arguments_json='{\"source_ref\":\"local-textbook\"}'")
        elif fault=='material_ref': c.execute("UPDATE agent_step SET arguments_json='{\"artifact_id\":\"local-page\"}'")
        elif fault=='original_change': c.execute("UPDATE capture_artifact SET extracted_text='Another response'")
        elif fault=='late_goal': c.execute("UPDATE agent_objective SET created_at='2099-01-01T00:00:00+00:00'")
        else: c.execute("INSERT INTO consent_record VALUES('retract','cloud_model_text_allowed',0,'privacy_v1','parent_ui','2026-09-08T00:00:00+00:00')")
        result=revalidate_formative_projection(c,projection_version=good['projection_version'],**scope)
        assert result['status']=='local_required' and result['payload'] is None,result
        c.rollback()


def test_privacy_redaction_never_rewrites_the_original_response(writing):
    from home_study_agent.writing_response_origin import WritingOriginalService
    db,sid,_,p,_=writing
    text=TEXT+' My email is synthetic@example.test.'
    original=WritingOriginalService(db).capture_native_response(sid,prompt_id=p['prompt_id'],item_version=p['item_version'],text=text,client_action_id='text')
    scored=answer(writing,text)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        bind(c,writing,original)
        c.commit()
    PrivacyLifecycleService(db,db.path.parent).initialize()
    scope=objective(db,sid,writing[3],scored['attempt'])
    with db.connect() as c:
        result=build_formative_projection(c,**scope)
        assert result['status']=='ready_for_runtime_checks',result
        assert 'synthetic@example.test' not in result['payload']['response']
        assert result['local_audit']['redaction_count']==1
        assert c.execute('SELECT submitted_answer FROM diagnostic_attempt').fetchone()[0]==text


@pytest.mark.parametrize('fault',[None,'missing_source','confirmation_source','stale_capture'])
def test_spoken_projection_uses_real_runtime_original_not_latest_recording(media,fault):
    import asyncio
    from test_agent_continuation import orchestrator_for
    from test_pet_media_evidence import speaking
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    db,_,sid,_,path,_=media
    p,text=speaking(media,transcript='我的答案是 I like reading in the library because it is quiet.')
    agent,gateway=orchestrator_for(db,path.parent,{})
    heard=asyncio.run(agent.interpret(sid,text,'original',False,voice_input_id='voice'))
    assert heard['action'].get('source_voice_input_id')=='voice',heard['action']
    scored=DiagnosticPromptService(db).answer(sid,prompt_id=p['prompt_id'],item_version=p['item_version'],
        answer=text,client_action_id='answer',authorized_turn_id=heard['agent_turn_id'])
    PrivacyLifecycleService(db,db.path.parent).initialize()
    scope=objective(db,sid,p,scored['attempt'])
    with db.connect() as c:
        good=build_formative_projection(c,**scope)
        assert good['status']=='ready_for_runtime_checks',good
        assert good['local_audit']['original']['source_voice_input_id']=='voice'
        assert not gateway.requests
        if fault is None:return
        if fault=='missing_source':c.execute("UPDATE agent_turn SET action_json=json_remove(action_json,'$.source_voice_input_id') WHERE agent_turn_id=?",(heard['agent_turn_id'],))
        elif fault=='confirmation_source':c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.source_voice_input_id','confirmation-voice') WHERE agent_turn_id=?",(heard['agent_turn_id'],))
        else:c.execute("UPDATE voice_capture_binding SET question_validation='stale'")
        result=revalidate_formative_projection(c,projection_version=good['projection_version'],**scope)
        assert result['status']=='local_required' and result['payload'] is None,result


def test_confirmed_page_authorizes_local_written_use_not_cloud(ocr):
    from test_writing_ocr_authorization import readback,authorize,confirm_turn,original
    from home_study_agent.writing_response_origin import bind_written_attempt
    writing,_,p=ocr
    db,sid,prompts,_,_=writing
    readback(ocr)
    readback(ocr,'ended')
    wrapper=original(ocr,authorize(ocr,confirm_turn(ocr)))
    scored=prompts.answer(sid,prompt_id=p['prompt_id'],item_version=p['item_version'],answer=TEXT,client_action_id='answer')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        bind_written_attempt(c,child_id=CHILD,session_id=sid,prompt_id=p['prompt_id'],item_version=p['item_version'],
            attempt_id=scored['attempt']['attempt_id'],original_artifact_id=wrapper['artifact_id'])
        c.execute("UPDATE agent_objective SET status='completed'")
        c.commit()
    PrivacyLifecycleService(db,db.path.parent).initialize()
    scope=objective(db,sid,writing[3],scored['attempt'])
    with db.connect() as c:
        result=build_formative_projection(c,**scope)
        assert result['status']=='local_required' and result['reason']=='WRITING_PAPER_PARENT_TEXT_CONSENT_REQUIRED',result


from test_writing_ocr_authorization import ocr
