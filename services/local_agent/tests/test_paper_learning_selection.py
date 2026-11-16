"""Real SQLite/public parent review pipeline; synthetic original items, no models."""
import json
from uuid import uuid4
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from home_study_agent.agent_runtime import AgentRuntimeStore,AgentPlanDecision,ObjectiveDraft,PlannedStep,CapabilityRegistry
from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.curriculum_supply_http import create_curriculum_supply_router
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService,DiagnosticPromptError
from home_study_agent.learning_input_revision import LearningInputRevisionService
from home_study_agent.paper_learning_selection import PaperLearningSelectionService,selected_for_voice
from home_study_agent.paper_learning_selection_http import create_paper_learning_selection_router
from home_study_agent.parent_auth import ParentAuth
from home_study_agent.privacy_operation_receipt import generation,preserve_live_identities
from home_study_agent.repository import StudyRepository
from test_curriculum_first_source_http import first_source_fixture,publish,CHILD,BASE


@pytest.fixture
def flow(tmp_path):
    db,source,spec,original=first_source_fixture(tmp_path)
    auth=ParentAuth(db);token=auth.setup('7391')
    app=FastAPI();app.include_router(create_curriculum_supply_router(CurriculumSupplyService(db),auth))
    app.include_router(create_paper_learning_selection_router(db,auth))
    client=TestClient(app,client=('127.0.0.1',41200),headers={'X-HSC-Parent':token})
    packet=publish(client,spec)
    sid=StudyRepository(db).start_session(CHILD,'start')['session_id']
    prompts=DiagnosticPromptService(db)
    inputs=LearningInputRevisionService(db);inputs.initialize()
    return db,client,packet,sid,prompts,inputs


def command(flow,text='做这张卷',*,malformed=None):
    db,_,_,sid,_,inputs=flow;key=str(uuid4());turn=str(uuid4());arrival=inputs.capture_arrival(sid,key,text)
    cap=CapabilityRegistry().require('diagnostic.start','study');store=AgentRuntimeStore(db)
    with db.connect() as c:
        c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,tool_status,orchestrator_version,created_at,client_action_id)
            VALUES (?,?,?,?,'agent_plan','','{"type":"start_diagnostic","track":"school"}','pending','test',datetime('now'),?)''',(turn,CHILD,sid,text,key))
        store.persist_plan(c,child_id=CHILD,session_id=sid,agent_turn_id=turn,client_action_id=key,
            context_manifest={'learning_input_arrival_id':arrival['arrival_id']},decision=AgentPlanDecision(
                ObjectiveDraft('none',None,None,(),'check'),PlannedStep(cap,{'track':'school'},'来做原题',cap.expected_evidence),'',True,1))
        c.execute("UPDATE agent_step SET status='running' WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)",(turn,))
        c.execute("UPDATE agent_plan SET status='running' WHERE agent_turn_id=?",(turn,))
        if malformed:malformed(c,turn)
        c.commit()
    return turn


def path(child=CHILD):return f'/api/parent/children/{child}/paper-learning-selection'
def body(flow,*,action='choose',operation='replace',version=0):
    db,_,packet,sid,_,_=flow
    with db.connect() as c:g=generation(c)
    return dict(session_id=sid,action_id=action,operation=operation,expected_version=version,expected_generation=g,
                **({} if operation=='clear' else dict(candidate_id=packet['candidate_id'],candidate_version=packet['version'])))
def choose(flow,**kwargs):
    result=flow[1].post(path(),json=body(flow,**kwargs));assert result.status_code==200,result.text;return result.json()
def ask(flow,turn,key='ask'):
    return flow[4].prepare(flow[3],paper_authorized_turn_id=turn,require_unexposed=True,client_action_id=key)


def test_publication_not_intent_parent_select_then_voice_prepare_restart(flow):
    db,client,packet,sid,_,_=flow
    assert client.get(path()).json()['sessions'][0]['selection']['items']==[]
    assert ask(flow,command(flow))['reason']=='PAPER_SELECTION_REQUIRED'
    selected=choose(flow)
    assert PaperLearningSelectionService(Database(db.path)).options(CHILD)['sessions'][0]['selection']['items']==selected['items']
    prepared=ask(flow,command(flow),'ask-after-selection')
    assert prepared['status']=='waiting_answer'
    assert prepared['prompt']['diagnostic_item_id']==packet['diagnostic_item_id']
    assert DiagnosticPromptService(Database(db.path)).read(sid)['prompt']['prompt_id']==prepared['prompt']['prompt_id']
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM paper_learning_selection_event').fetchone()[0]==1


def test_http_retry_idempotent_and_pending_question_never_skips(flow):
    db,client,packet,sid,prompts,_=flow;request=body(flow)
    assert client.post(path(),json=request).json()==client.post(path(),json=request).json()
    turn=command(flow);first=ask(flow,turn);assert ask(flow,turn)==first
    with pytest.raises(DiagnosticPromptError,match='pending_prompt_exists'):ask(flow,command(flow,'下一题'),'next-before-answer')
    assert prompts.read(sid)['prompt']['prompt_id']==first['prompt']['prompt_id']
    with db.connect() as c:assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]==1


def test_normal_parent_auth_loopback_strict_fields_and_other_child(flow):
    _,client,_,_,_,_=flow;token=client.headers.pop('X-HSC-Parent')
    assert client.get(path()).status_code==401
    assert client.post(path(),json=body(flow)).status_code==401
    client.headers['X-HSC-Parent']=token
    assert client.post(path(),json={**body(flow),'actor':'parent'}).status_code==422
    assert client.get(path()+'?hidden=true').status_code==422
    assert client.post(path('child-primary-4'),json=body(flow)).status_code==422
    remote=TestClient(client.app,client=('192.0.2.1',41000),headers={'X-HSC-Parent':token})
    assert remote.post(path(),json=body(flow)).status_code==403


def test_publication_version_and_withdrawal_no_silent_fallback(flow):
    db,client,packet,sid,prompts,_=flow;stale=body(flow);stale['candidate_version']-=1
    assert client.post(path(),json=stale).status_code==422
    choose(flow);result=ask(flow,command(flow));pid=result['prompt']['prompt_id']
    response=client.post(BASE+f"/{packet['candidate_id']}/withdraw",json=dict(action_id='withdraw',expected_version=packet['version'],note='撤回合成原题'))
    assert response.status_code==200,response.text
    assert prompts.read(sid)['status']=='invalidated'
    assert prompts.read(sid)['prompt']['prompt_id']==pid
    assert ask(flow,command(flow,'下一题'),'next-withdrawn')['reason']=='PAPER_SELECTION_SOURCE_UNAVAILABLE'
    assert client.get(path()).json()['sessions'][0]['selection']['status']=='blocked'


def test_change_after_voice_rejected_clear_does_not_cancel_pending(flow):
    _,_,_,sid,prompts,_=flow;choose(flow);turn=command(flow)
    choose(flow,action='clear',operation='clear',version=1)
    assert ask(flow,turn)['reason']=='PAPER_SELECTION_ARRIVAL_CHANGED'
    choose(flow,action='choose2',version=2);first=ask(flow,command(flow),'new')
    choose(flow,action='clear2',operation='clear',version=3)
    assert prompts.read(sid)['prompt']['prompt_id']==first['prompt']['prompt_id']
    assert prompts.read(sid)['status']=='waiting_answer'


def test_restore_invalidates_saved_selection_prompt_and_uncommitted_old_request(flow):
    db,client,_,sid,prompts,_=flow;stale=body(flow,action='not-submitted');choose(flow);ask(flow,command(flow))
    with db.connect() as live,db.connect() as staged:preserve_live_identities(live,staged);staged.commit()
    assert client.get(path()).json()['sessions'][0]['selection']['reason']=='PAPER_SELECTION_RESTORE_RESELECT_REQUIRED'
    assert client.post(path(),json=stale).json()['detail']['code']=='PAPER_SELECTION_RESTORE_RESELECT_REQUIRED'
    assert prompts.read(sid)['reason']=='PAPER_SELECTION_RESTORE_RESELECT_REQUIRED'
    assert choose(flow,action='after-restore',version=1)['status']=='selected'


@pytest.mark.parametrize('text',['他说做这张卷','不要做这张卷','这张卷吗','做这张卷然后玩游戏','下一题的答案是什么'])
def test_negative_quote_ambiguous_mixed_speech_does_not_activate(flow,text):
    db,_,_,sid,_,_=flow;choose(flow);turn=command(flow,text)
    from home_study_agent.local_supply import LocalSupplyError
    with db.connect() as c:
        try:assert selected_for_voice(c,DiagnosticPromptService._session(c,sid),turn) is None
        except LocalSupplyError as e:assert str(e)=='PAPER_SELECTION_INTENT_UNCLEAR'


@pytest.mark.parametrize('mutation,reason',[
    (lambda c,t:c.execute("UPDATE agent_turn SET tool_status='completed' WHERE agent_turn_id=?",(t,)),'PAPER_SELECTION_VOICE_NOT_EXECUTABLE'),
    (lambda c,t:c.execute("UPDATE agent_turn SET intent='runtime_continuation' WHERE agent_turn_id=?",(t,)),'PAPER_SELECTION_VOICE_NOT_EXECUTABLE'),
    (lambda c,t:c.execute("UPDATE learning_input_arrival SET utterance='other'"),'PAPER_SELECTION_VOICE_SUPERSEDED'),
    (lambda c,t:c.execute("UPDATE agent_plan SET context_manifest_json='{}' WHERE agent_turn_id=?",(t,)),'PAPER_SELECTION_VOICE_SUPERSEDED'),
    (lambda c,t:c.execute("UPDATE agent_step SET arguments_json='{\"diagnostic_item_id\":\"forged\"}'"),'PAPER_SELECTION_VOICE_BINDING_CHANGED'),
])
def test_requires_actual_current_running_authorized_turn(flow,mutation,reason):
    choose(flow);turn=command(flow,malformed=mutation);assert ask(flow,turn)['reason']==reason
    with flow[0].connect() as c:assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]==0


def test_duplicate_cas_superseded_retry_and_child_isolation(flow):
    db,client,_,_,_,_=flow;choose(flow)
    assert client.post(path(),json=body(flow,action='append',operation='append',version=1)).json()['detail']['code']=='PAPER_SELECTION_DUPLICATE'
    assert client.post(path(),json=body(flow,action='stale')).json()['detail']['code']=='PAPER_SELECTION_VERSION_CONFLICT'
    choose(flow,action='clear',operation='clear',version=1)
    assert client.post(path(),json=body(flow)).json()['detail']['code']=='PAPER_SELECTION_SUPERSEDED'
    from home_study_agent.document_monitoring import DocumentMonitoringService
    DocumentMonitoringService(db)
    other=StudyRepository(db).switch_active_session_child(flow[3],'child-primary-4','other')['session_id'];forged=body(flow,action='cross-child');forged['session_id']=other
    assert client.post(path(),json=forged).json()['detail']['code']=='PAPER_SELECTION_SESSION_CHANGED'
    assert client.get(path('child-primary-4')).json()['sessions'][0]['selection']['items']==[]


def test_failed_event_rolls_back_selection_and_exact_retry(flow):
    import sqlite3
    db,_,_,sid,_,_=flow
    with db.connect() as c:
        c.execute("CREATE TRIGGER fail_selection BEFORE INSERT ON paper_learning_selection_event BEGIN SELECT RAISE(ABORT,'injected failure'); END");c.commit()
    service=PaperLearningSelectionService(db);req=body(flow);req.pop('session_id')
    with pytest.raises(sqlite3.IntegrityError,match='injected'):service.change(CHILD,sid,**req)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM paper_learning_selection').fetchone()[0]==0
        c.execute('DROP TRIGGER fail_selection');c.commit()
    assert service.change(CHILD,sid,**req)['version']==1


def test_no_selection_next_and_ordinary_request_preserve_original_pool(flow):
    db,_,_,sid,_,_=flow
    for text in ['下一题','来一道题']:
        turn=command(flow,text)
        with db.connect() as c:assert selected_for_voice(c,DiagnosticPromptService._session(c,sid),turn) is None


def test_export_preview_delete_cover_reference_and_event(flow,tmp_path):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    db,_,_,_,_,_=flow;choose(flow)
    privacy=PrivacyLifecycleService(db, tmp_path, b'0' * 32);privacy.initialize()
    exported=privacy.export_child(CHILD)
    with open(exported['path'],encoding='utf-8') as stream:payload=json.load(stream)
    assert len(payload['paper_learning_selections'])==len(payload['paper_learning_selection_events'])==1
    assert privacy.deletion_preview(CHILD)['paper_learning_selections']==2
    assert privacy.delete_child_scope(CHILD,'learning','确认删除','delete-selection')['status']=='completed'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM paper_learning_selection').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM paper_learning_selection_event').fetchone()[0]==0
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_real_app_http_two_published_originals_prepare_replay_and_concurrent_next(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from test_curriculum_first_source_http import app_client,auth
    db,source,spec,_=first_source_fixture(tmp_path,with_parallel_original=True)
    with app_client(tmp_path,db) as client:
        auth(client);first=publish(client,spec)
        options=client.get(BASE+f"/sources/{source['source_id']}/options").json()
        second=next(r for r in options['excerpts'] if r['excerpt_id']!=spec['excerpt_id'])
        seed={k:v for k,v in spec.items() if k!='knowledge_node_id'}
        seed.update(seed_item_id=first['diagnostic_item_id'],excerpt_id=second['excerpt_id'],stage='parallel',validator='curriculum_objective_exact_source_v1')
        response=client.post(BASE+'/generate',json=dict(action_id='second-d',expected_version=0,expected_scope_version=1,source=seed))
        assert response.status_code==200,response.text
        cid=response.json()['candidate_id']
        for version,kind in enumerate(('validate','review','publish'),1):
            values=dict(action_id='second-'+kind,expected_version=version)
            if kind=='review':values.update(decision='approve',note='核对独立创作的第二题及完整答案，仅平行题。')
            response=client.post(BASE+f'/{cid}/{kind}',json=values);assert response.status_code==200,response.text
        second=response.json()
        sid=client.post('/v1/sessions',json=dict(child_id=CHILD,client_action_id='paper-session')).json()['session_id']
        prompts=DiagnosticPromptService(db);inputs=LearningInputRevisionService(db);inputs.initialize()
        flow=(db,client,first,sid,prompts,inputs);choose(flow)
        selected=body(flow,action='append-second',version=1,operation='append')
        selected.update(candidate_id=second['candidate_id'],candidate_version=second['version'])
        assert client.post(path(),json=selected).status_code==200
        turn=command(flow);endpoint=f'/v1/sessions/{sid}/diagnostic-prompt/prepare'
        payload=dict(track='school',require_unexposed=True,paper_authorized_turn_id=turn,client_action_id='http-first')
        response=client.post(endpoint,json=payload);assert response.status_code==200,response.text
        prepared=response.json();assert prepared['prompt']['diagnostic_item_id']==first['diagnostic_item_id']
        assert client.post(endpoint,json=payload).json()['prompt']['prompt_id']==prepared['prompt']['prompt_id']
        # A distinct actual child request cannot bypass the open prompt.
        next_turn=command(flow,'下一题')
        conflict=client.post(endpoint,json={**payload,'paper_authorized_turn_id':next_turn,'client_action_id':'premature-next'})
        assert conflict.status_code==409
        saved=prompts.answer(sid,prompt_id=prepared['prompt']['prompt_id'],item_version=prepared['prompt']['item_version'],answer='2,3',client_action_id='first-answer')
        assert saved['status']=='answered' and saved['attempt']['score_status']=='verified_local'
        next_turn=command(flow,'下一题')
        def concurrent(key):
            try:return prompts.prepare(sid,paper_authorized_turn_id=next_turn,require_unexposed=True,client_action_id=key)
            except DiagnosticPromptError as e:return e.code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(concurrent,['next-a','next-b']))
        passed=[v for v in results if isinstance(v,dict)]
        assert len(passed)==1 and results.count('pending_prompt_exists')==1
        assert passed[0]['prompt']['diagnostic_item_id']==second['diagnostic_item_id']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]==2
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
        restarted=DiagnosticPromptService(Database(db.path))
        assert restarted.read(sid)['prompt']['prompt_id']==passed[0]['prompt']['prompt_id']
