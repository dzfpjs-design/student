"""Real isolated services/HTTP/runtime; synthetic WAV, callbacks and planner."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import home_study_agent
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.diagnostic_prompt import DiagnosticPromptService, DiagnosticPromptError
from home_study_agent.exam_simulation import ExamSimulationService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.model_gateway import ModelResult
from home_study_agent.agent_runtime import InvalidAgentPlanError
from test_school_listening_modality import prepared_school,publish_school,play,CHILD
from test_pet_listening_resource_admission import bind_audio
from test_pet_vocabulary_prompt_integration import Planner,turn

BALANCED_ITEMS = ExamSimulationService._balanced_items


def sufficient_full_school_fixture(case,monkeypatch):
    db,sid,service,run,client,planner,prompt,source,mono,root=case
    with db.connect() as c:
        seed=c.execute('SELECT diagnostic_item_id FROM specialized_exam_item WHERE run_id=? AND position=2',(run['run_id'],)).fetchone()[0]
        c.execute('''INSERT INTO diagnostic_item(diagnostic_item_id,knowledge_node_id,prompt,answer_type,
            expected_answer_json,explanation,assessment_mode,difficulty,source_ref,status,stimulus_text,stimulus_asset)
            SELECT 'isolated-full-school-capacity',knowledge_node_id,prompt || ' [isolated full-capacity fixture]',answer_type,
            expected_answer_json,explanation,assessment_mode,difficulty,'test-blueprint-capacity','active',stimulus_text,stimulus_asset
            FROM diagnostic_item WHERE diagnostic_item_id=?''',(seed,))
        c.execute('''INSERT INTO diagnostic_item(diagnostic_item_id,knowledge_node_id,prompt,answer_type,
            expected_answer_json,explanation,assessment_mode,difficulty,source_ref,status,stimulus_text,stimulus_asset)
            SELECT 'isolated-full-school-capacity-two',knowledge_node_id,prompt || ' [second isolated capacity fixture]',answer_type,
            expected_answer_json,explanation,assessment_mode,difficulty,'test-blueprint-capacity','active',stimulus_text,stimulus_asset
            FROM diagnostic_item WHERE diagnostic_item_id=?''',(seed,))
        c.commit()
    monkeypatch.setattr(ExamSimulationService,'_balanced_items',staticmethod(BALANCED_ITEMS))


class Gateway(Planner):
    async def complete(self,request):
        if request.request_type == 'voice_interaction':
            self.requests.append(request)
            transcript = json.loads(request.messages[-1]['content'])['transcript']
            capability = self.payload['next_step']['capability']
            control = capability if capability in {'exam.pause','exam.resume','exam.cancel'} else None
            role='control' if control else 'question' if capability=='exam.start' else 'answer'
            return ModelResult(content=json.dumps({'role':role,'control':control,
                'quote':transcript,'confidence':.95}),provider='test',model='synthetic-intent',latency_ms=1)
        return await super().complete(request)


@pytest.fixture
def case(tmp_path,monkeypatch):
    assert Path(home_study_agent.__file__).resolve() == Path(__file__).resolve().parents[1] / 'src/home_study_agent/__init__.py'
    db,supply,spec,source,source_id = prepared_school(tmp_path)
    item_id = publish_school(supply,spec)['diagnostic_item_id']
    with db.connect() as c:
        other = c.execute("""SELECT di.diagnostic_item_id FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id)
            WHERE kn.track='school' AND kn.subject='英语' AND kn.grade_band='初三' AND di.answer_type!='listening_text'
            AND di.status='active' ORDER BY di.diagnostic_item_id LIMIT 1""").fetchone()[0]
        selected = [dict(c.execute('''SELECT di.diagnostic_item_id,kn.knowledge_node_id,kn.subject,kn.title
            FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id) WHERE di.diagnostic_item_id=?''',(key,)).fetchone())
            for key in (item_id,other)]
    from home_study_agent.specialized_program import SpecializedProgramService
    selected_domains=[SpecializedProgramService._mastery_domain('junior_exam',item) for item in selected]
    monkeypatch.setattr(ExamSimulationService,'_balanced_items',staticmethod(lambda *args:list(zip(selected_domains,selected))))
    mono = [1000]
    monkeypatch.setattr('home_study_agent.pet_media_evidence._clock_ms',lambda:mono[0])
    settings = Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False,agent_runtime_enabled=True)
    planner = Gateway()
    app = create_app(settings,model_gateway=planner)
    with TestClient(app,client=('127.0.0.1',42421)) as client:
        assert client.post('/v1/parent/setup',json={'pin':'2468'}).status_code==200
        sid = client.post('/v1/sessions',json={'child_id':CHILD,'client_action_id':'exam-school-session'}).json()['session_id']
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,))
            c.commit()
        service = ExamSimulationService(db)
        run = service.start(sid,'junior_exam','英语','practice',2,30,'prepare-real-listening-paper')
        token = client.post('/v1/parent/unlock',json={'pin':'2468'}).json()['parent_token']
        parents = client.get(f'/v1/parent/children/{CHILD}/pet-prompts',headers={'X-HSC-Parent':token}).json()['prompts']
        prompt, = parents
        yield db,sid,service,run,client,planner,prompt,source,mono,tmp_path


def activate(case,key='explicit-start'):
    db,sid,service,run,client,planner,prompt,source,mono,root = case
    preparation = service.status(run['run_id'])
    assert preparation['delivery_status'] in {'ready','paused'}, preparation['delivery_blockers']
    planner.select('exam.resume','继续模拟卷',arguments={})
    planned = turn(client,sid,'继续模拟卷',key)
    response = client.post(f"/v1/agent-turns/{planned['agent_turn_id']}/execute")
    with db.connect() as c:
        current = c.execute('SELECT control_revision,ready_at,delivery_phase FROM specialized_exam_run WHERE run_id=?',(run['run_id'],)).fetchone()
        arrival = c.execute('SELECT created_at,snapshot_json FROM learning_input_arrival WHERE client_action_id=?',(key,)).fetchone()
        debug = dict(current=dict(current),created_at=arrival['created_at'],snapshots=[x for x in json.loads(arrival['snapshot_json']) if x.get('kind')=='exam_listening_control'])
    assert response.status_code==200,response.text+' '+json.dumps(debug)
    active = service.status(run['run_id'])
    assert active['status']=='active' and active['delivery_status']=='active',active
    return active,planned


def answer(case,key='original-exam-answer'):
    db,sid,service,run,client,planner,prompt,source,mono,root = case
    words = source['expected_answer']['exact_answers'][0]
    planner.select('exam.submit',words,role='answer',arguments={'answer':words})
    planned = turn(client,sid,words,key)
    response = client.post(f"/v1/agent-turns/{planned['agent_turn_id']}/execute")
    if planned['intent']=='pending_answer_clarification':
        assert response.status_code==200,response.text
        planned = turn(client,sid,'刚才那句是我的答案',key+'-confirm')
        response = client.post(f"/v1/agent-turns/{planned['agent_turn_id']}/execute")
    return response,planned


def test_preparation_is_durable_untimed_and_media_owner_is_not_a_diagnostic(case):
    db,sid,service,run,client,planner,prompt,source,mono,root = case
    assert run['status']=='paused' and run['delivery_status']=='preparing'
    assert run['total_items']==2 and run['elapsed_seconds']==0 and run['started_at'] is None
    assert run['current_item']['stimulus_text'] is None
    assert source['stimulus_text'] not in json.dumps(run,ensure_ascii=False)
    assert prompt['item']['stimulus_text']==source['stimulus_text'] and prompt['exam_slot']['run_id']==run['run_id']
    ordinary = DiagnosticPromptService(db)
    assert ordinary.read(sid)['prompt'] is None
    for action in (
        lambda:ordinary.read(sid,prompt_id=prompt['prompt_id']),
        lambda:ordinary.answer(sid,prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],answer='library',client_action_id='bypass'),
        lambda:ordinary.resume(sid,prompt_id=prompt['prompt_id']),
        lambda:ordinary.cancel(sid,prompt_id=prompt['prompt_id'],item_version=prompt['item_version'])):
        with pytest.raises(DiagnosticPromptError,match='exam_prompt_requires_exam_submit'):
            action()
    with pytest.raises(ValueError,match='exam_item_requires_exam_submit'):
        LearningStateService(db).submit_diagnostic(sid,prompt['diagnostic_item_id'],'library','bare-bypass')
    media,_ = bind_audio(client,db,root,sid,prompt,key='exam-original')
    result = client.get(f"/v1/sessions/{sid}/pet-media/{media['media_id']}/audio",params={'audio_sha256':media['audio']['sha256']})
    assert result.status_code==409 and 'EXAM_MEDIA_RUN_NOT_ACTIVE' in result.text
    ready = service.status(run['run_id'])
    assert ready['delivery_status']=='ready' and ready['elapsed_seconds']==0
    assert service.recover_after_restart()==0
    assert ExamSimulationService(db).status(run['run_id'])['started_at'] is None
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM specialized_exam_item WHERE run_id=?',(run['run_id'],)).fetchone()[0]==2


def test_first_child_resume_after_parent_approval_requires_no_status_poll(case):
    db,sid,service,run,client,planner,prompt,source,mono,root = case
    media,_ = bind_audio(client,db,root,sid,prompt,key='first-resume-without-status')
    with db.connect() as c:
        prepared = dict(c.execute('SELECT * FROM specialized_exam_run WHERE run_id=?',(run['run_id'],)).fetchone())
        reviewed_at = c.execute("SELECT created_at FROM pet_media_event WHERE media_id=? AND operation='review_source'",
            (media['media_id'],)).fetchone()[0]
    planner.select('exam.resume','继续模拟卷',arguments={})
    planned = turn(client,sid,'继续模拟卷','first-resume-after-approval')
    response = client.post(f"/v1/agent-turns/{planned['agent_turn_id']}/execute")
    assert response.status_code == 200, response.text
    actual = response.json()['execution']['result']
    assert actual['status'] == 'active' and actual['delivery_status'] == 'active', actual
    with db.connect() as c:
        arrival = c.execute('SELECT created_at,snapshot_json FROM learning_input_arrival WHERE client_action_id=?',
            ('first-resume-after-approval',)).fetchone()
        frozen = next(x['record'] for x in json.loads(arrival['snapshot_json']) if x.get('kind')=='exam_listening_control')
        assert prepared['delivery_phase'] == 'ready' and prepared['activated_at'] is None
        assert prepared['active_since'] is None and prepared['elapsed_seconds'] == 0
        assert run['started_at'] is None
        assert reviewed_at <= prepared['ready_at'] <= arrival['created_at'] <= actual['started_at']
        assert frozen['ready_version'] == prepared['ready_version']
        assert frozen['control_revision'] == prepared['control_revision']
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM pet_media_event WHERE operation='playback'").fetchone()[0] == 0


@pytest.mark.parametrize('change', ['approve_after_arrival','approval_revision','source_withdrawal'])
def test_parent_readiness_changes_after_arrival_cannot_activate_old_child_grant(case, change):
    db,sid,service,run,client,planner,prompt,source,mono,root = case
    media,_ = bind_audio(client,db,root,sid,prompt,approved=change!='approve_after_arrival',key='late-review-original')
    planner.select('exam.resume','继续模拟卷',arguments={})
    planned = turn(client,sid,'继续模拟卷','frozen-before-review-change')
    token = client.post('/v1/parent/unlock',json={'pin':'2468'}).json()['parent_token']
    if change == 'source_withdrawal':
        with db.connect() as c:
            candidate = c.execute('SELECT source_ref FROM diagnostic_item WHERE diagnostic_item_id=?',
                (prompt['diagnostic_item_id'],)).fetchone()[0].partition(':')[2]
        changed = client.post(f'/api/parent/children/{CHILD}/curriculum-supply/{candidate}/withdraw',
            headers={'X-HSC-Parent':token},json=dict(action_id='withdraw-after-arrival',expected_version=4,
                note='Synthetic reviewed original withdrawn after the frozen child request.'))
    else:
        changed = client.post(f"/v1/sessions/{sid}/pet-media/{media['media_id']}/source-review",
            headers={'X-HSC-Parent':token},json=dict(audio_sha256=media['audio']['sha256'],operation='approve',full_spoken_reference=True,
                client_action_id='actual-review-after-arrival'))
    assert changed.status_code == 200, changed.text
    response = client.post(f"/v1/agent-turns/{planned['agent_turn_id']}/execute")
    assert response.status_code == 409, response.text
    if change != 'source_withdrawal':
        assert response.json()['code'] == 'EXAM_LISTENING_START_GRANT_STALE'
    with db.connect() as c:
        current = c.execute('SELECT * FROM specialized_exam_run WHERE run_id=?',(run['run_id'],)).fetchone()
        arrival = c.execute('SELECT created_at FROM learning_input_arrival WHERE client_action_id=?',
            ('frozen-before-review-change',)).fetchone()[0]
        assert current['status'] == 'paused' and current['activated_at'] is None
        assert current['active_since'] is None and current['elapsed_seconds'] == 0
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM pet_media_event WHERE operation='playback'").fetchone()[0] == 0
        if change != 'source_withdrawal':
            assert arrival < current['ready_at']
            step = c.execute('''SELECT s.status,s.error_code FROM agent_step s JOIN agent_plan p USING(plan_id)
                WHERE p.agent_turn_id=? AND s.step_index=0''',(planned['agent_turn_id'],)).fetchone()
            assert tuple(step) == ('failed','EXAM_LISTENING_START_GRANT_STALE')


@pytest.mark.parametrize('error_kind', ['private_domain','private_other','untyped_known_code'])
def test_executor_preserves_only_exact_typed_safe_exam_error_code(case, monkeypatch, error_kind):
    from home_study_agent.exam_simulation import ExamSimulationError
    db,sid,service,run,client,planner,prompt,source,mono,root = case
    bind_audio(client,db,root,sid,prompt,key='safe-error-original')
    planner.select('exam.resume','继续模拟卷',arguments={})
    planned = turn(client,sid,'继续模拟卷','safe-exception-persistence')
    private = 'PRIVATE_SYNTHETIC_CONTEXT_MUST_NOT_ENTER_STEP'
    error = ExamSimulationError(private) if error_kind=='private_domain' else ValueError(
        'EXAM_LISTENING_START_GRANT_STALE' if error_kind=='untyped_known_code' else private)
    executor = client.app.state.agent_capability_executor
    def fail_dispatch(step):
        raise error
    monkeypatch.setattr(executor,'_dispatch',fail_dispatch)
    try:
        response = client.post(f"/v1/agent-turns/{planned['agent_turn_id']}/execute")
    except type(error):
        pass
    else:
        assert response.status_code >= 400, response.text
    with db.connect() as c:
        step = dict(c.execute('''SELECT s.* FROM agent_step s JOIN agent_plan p USING(plan_id)
            WHERE p.agent_turn_id=? AND s.step_index=0''',(planned['agent_turn_id'],)).fetchone())
        assert step['status'] == 'failed' and step['error_code'] == type(error).__name__
        assert private not in json.dumps(step) and step['error_code'] != 'EXAM_LISTENING_START_GRANT_STALE'
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0


def test_real_runtime_start_media_bytes_synthetic_callbacks_original_answer_unique_submit_and_recovery(case):
    db,sid,service,run,client,planner,prompt,source,mono,root = case
    media,_ = bind_audio(client,db,root,sid,prompt,key='exam-original')
    active,started = activate(case)
    delivered_bytes=client.get(f"/v1/sessions/{sid}/pet-media/{media['media_id']}/audio",params={'audio_sha256':media['audio']['sha256']})
    from hashlib import sha256
    assert delivered_bytes.status_code==200 and sha256(delivered_bytes.content).hexdigest()==media['audio']['sha256']
    from home_study_agent.pet_media_evidence import PetMediaEvidenceService
    media = PetMediaEvidenceService(db,media_root=root).list_for_prompt(sid,prompt_id=prompt['prompt_id'],item_version=prompt['item_version'])['listening'][0]
    media = play(client,sid,media,mono,'real-exam-play')
    response,planned = answer(case)
    assert response.status_code==200,response.text
    with db.connect() as c:
        attempts = list(c.execute('SELECT * FROM diagnostic_attempt'))
        assert len(attempts)==1 and attempts[0]['submitted_answer']==source['expected_answer']['exact_answers'][0],json.dumps({'planned':planned,'executed':response.json()},ensure_ascii=False)
        slot = c.execute('SELECT * FROM specialized_exam_item WHERE run_id=? AND position=1',(run['run_id'],)).fetchone()
        assert slot['attempt_id']==attempts[0]['attempt_id'] and slot['answer_arrived_at']
        carrier = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(prompt['prompt_id'],)).fetchone()
        assert carrier['status']=='answered' and carrier['attempt_id']==attempts[0]['attempt_id']
        assert carrier['authorized_turn_id']==planned['agent_turn_id']
        score = LearningStateService._attempt(c,attempts[0]['attempt_id'])
        assert score['correctness']==1 and score['score_status']=='verified_local',score
        assert service._current_attempt_score(c,c.execute('SELECT * FROM specialized_exam_run WHERE run_id=?',(run['run_id'],)).fetchone(),slot,attempts[0]['attempt_id'])==1
    replay = client.post(f"/v1/agent-turns/{planned['agent_turn_id']}/execute")
    assert replay.status_code==200 and replay.json()==response.json()
    assert service.status(run['run_id'])['current_position']==2
    service.recover_after_restart()
    recovered = service.status(run['run_id'])
    assert recovered['status']=='paused' and recovered['started_at']==active['started_at']
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==1


def test_unplayed_original_answer_cannot_be_reserved_then_upgraded_by_late_playback(case):
    db,sid,service,run,client,planner,prompt,source,mono,root = case
    media,_ = bind_audio(client,db,root,sid,prompt,key='exam-original')
    activate(case)
    response,planned = answer(case,'unplayed-original')
    assert response.status_code==409,response.text
    from home_study_agent.pet_media_evidence import PetMediaEvidenceService
    media = PetMediaEvidenceService(db,media_root=root).list_for_prompt(sid,prompt_id=prompt['prompt_id'],item_version=prompt['item_version'])['listening'][0]
    play(client,sid,media,mono,'late-play')
    retry = client.post(f"/v1/agent-turns/{planned['agent_turn_id']}/execute")
    assert retry.status_code==409,retry.text
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0
        assert c.execute('SELECT answer_action_id FROM specialized_exam_item WHERE run_id=? AND position=1',(run['run_id'],)).fetchone()[0] is None


@pytest.mark.parametrize('operation',['pause','cancel','restart','timeout','retract'])
def test_current_run_lifecycle_invalidates_inflight_synthetic_callback(case,operation):
    from home_study_agent.pet_media_evidence import PetMediaEvidenceService
    db,sid,service,run,client,planner,prompt,source,mono,root = case
    media,_ = bind_audio(client,db,root,sid,prompt,key='exam-original')
    active,_ = activate(case)
    media = PetMediaEvidenceService(db,media_root=root).list_for_prompt(sid,prompt_id=prompt['prompt_id'],item_version=prompt['item_version'])['listening'][0]
    result = client.post(f'/v1/sessions/{sid}/pet-media/listening/control',json=dict(media_id=media['media_id'],
        generation=media['generation'],expected_revision=media['revision'],command='resume',client_action_id='inflight-start'))
    assert result.status_code==200,result.text
    media=result.json()['media']
    started = client.post(f'/v1/sessions/{sid}/pet-media/listening/playback',json=dict(media_id=media['media_id'],
        generation=media['generation'],expected_revision=media['revision'],event='started',audio_sha256=media['audio']['sha256'],
        frame_position=0,monotonic_ms=mono[0],client_action_id='actual-started'))
    assert started.status_code==200,started.text
    media=started.json()['media']
    if operation=='restart':
        assert service.recover_after_restart()==1
    elif operation=='timeout':
        with db.connect() as c:
            c.execute("UPDATE specialized_exam_section_state SET active_since='2020-01-01T00:00:00+00:00' WHERE run_id=? AND status='active'",(run['run_id'],))
            c.commit()
        assert service.status(run['run_id'])['status']=='timed_out'
    elif operation=='retract':
        token=client.post('/v1/parent/unlock',json={'pin':'2468'}).json()['parent_token']
        response=client.post(f"/v1/sessions/{sid}/pet-media/{media['media_id']}/source-review",headers={'X-HSC-Parent':token},
            json=dict(audio_sha256=media['audio']['sha256'],operation='retract',client_action_id='withdraw-original'))
        assert response.status_code==200,response.text
        assert service.status(run['run_id'])['status']=='paused'
    else:
        getattr(service,operation)(run['run_id'])
    mono[0]+=1000
    late=client.post(f'/v1/sessions/{sid}/pet-media/listening/playback',json=dict(media_id=media['media_id'],
        generation=media['generation'],expected_revision=media['revision'],event='ended',audio_sha256=media['audio']['sha256'],
        frame_position=media['audio']['frames'],monotonic_ms=mono[0],client_action_id='late-ended'))
    assert late.status_code==409,late.text
    assert service.status(run['run_id'])['audio_delivery']['playback_allowed'] is False
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM pet_media_event WHERE operation='playback' AND json_extract(payload_json,'$.event')='ended'").fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0


def test_fresh_explicit_resume_preserves_first_start_and_old_grant_cannot_reactivate(case):
    from home_study_agent.exam_simulation import ExamSimulationError
    db,sid,service,run,client,planner,prompt,source,mono,root = case
    bind_audio(client,db,root,sid,prompt,key='exam-original')
    active,first_grant=activate(case)
    service.pause(run['run_id'])
    with pytest.raises(ExamSimulationError,match='EXAM_LISTENING_START_GRANT_STALE'):
        service.resume(run['run_id'],authorized_turn_id=first_grant['agent_turn_id'])
    assert service.status(run['run_id'])['status']=='paused'
    resumed,_=activate(case,'fresh-resume')
    assert resumed['started_at']==active['started_at']
    assert resumed['audio_delivery']['control_revision']>active['audio_delivery']['control_revision']


def test_day_old_actual_resume_turn_is_rejected_even_with_same_run_snapshot(case):
    db,sid,service,run,client,planner,prompt,source,mono,root = case
    bind_audio(client,db,root,sid,prompt,key='exam-original')
    assert service.status(run['run_id'])['delivery_status']=='ready'
    planner.select('exam.resume','继续模拟卷',arguments={})
    planned=turn(client,sid,'继续模拟卷','old-grant')
    with db.connect() as c:
        c.execute("UPDATE learning_input_arrival SET created_at='2020-01-01T00:00:00+00:00' WHERE client_action_id='old-grant'")
        c.commit()
    response=client.post(f"/v1/agent-turns/{planned['agent_turn_id']}/execute")
    assert response.status_code==409 and response.json()['code']=='EXAM_LISTENING_START_GRANT_STALE'
    assert service.status(run['run_id'])['status']=='paused'


def test_existing_ordinary_draft_survives_listening_start_conflict_without_new_run(case,monkeypatch):
    from home_study_agent.exam_simulation import ExamSimulationError
    db,sid,service,run,client,planner,prompt,source,mono,root = case
    service.cancel(run['run_id'])
    sufficient_full_school_fixture(case,monkeypatch)
    with db.connect() as c:
        item_id=c.execute('SELECT diagnostic_item_id FROM specialized_exam_item WHERE run_id=? AND position=2',(run['run_id'],)).fetchone()[0]
    # A real start turn can arrive before a draft is prepared elsewhere; the
    # executor must recheck and explain that conflict without cancelling it.
    planner.select('exam.start','开始英语模拟卷',arguments=dict(track='junior_exam',subject='英语',mode='practice',requested_items=2))
    planned=turn(client,sid,'开始英语模拟卷','explicit-new-paper-conflict')
    assert planned['action']['type']=='start_exam_simulation',planned
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM agent_plan WHERE agent_turn_id=?',(planned['agent_turn_id'],)).fetchone()[0]==1,planned
    ordinary=DiagnosticPromptService(db)
    draft=ordinary.prepare(sid,track='school',preferred_item_id=item_id,client_action_id='real-original-draft')
    with pytest.raises(ExamSimulationError,match='EXAM_ORDINARY_DRAFT_PENDING'):
        service.start(sid,'junior_exam','英语','full',6,30,'conflicting-new-paper')
    assert ordinary.read(sid)==draft

    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM specialized_exam_run').fetchone()[0]==1
    response=client.post(f"/v1/agent-turns/{planned['agent_turn_id']}/execute")
    assert response.status_code==200,response.text
    spoken=response.json()['execution']['response_text']
    assert '保留' in spoken and '完成原题' in spoken and '取消原题' in spoken and 'EXAM_' not in spoken
    assert ordinary.read(sid)==draft
    early=turn(client,sid,'开始英语模拟卷','start-with-existing-original')
    assert early['action']['type']=='none' and early['action']['source']=='pending_question_conflict'
    assert '完成原题' in early['response_text'] and '保留' in early['response_text']
    assert ordinary.read(sid)==draft


def test_clear_voice_full_start_creates_real_plan_preparation_without_task_advancement(case,monkeypatch):
    db,sid,service,run,client,planner,prompt,source,mono,root=case
    service.cancel(run['run_id'])
    sufficient_full_school_fixture(case,monkeypatch)
    assert DiagnosticPromptService(db).read(sid)['prompt'] is None
    with db.connect() as c:
        tasks=[tuple(row) for row in c.execute('SELECT plan_item_id,status,started_at,completed_at FROM plan_item ORDER BY plan_item_id')]
        events=c.execute('SELECT count(*) FROM plan_item_event').fetchone()[0]
    planner.select('exam.start','开始英语模拟卷',arguments={})
    planned=turn(client,sid,'开始英语模拟卷','actual-voice-full-paper')
    assert planned['action']['type']=='start_exam_simulation',planned
    response=client.post(f"/v1/agent-turns/{planned['agent_turn_id']}/execute")
    assert response.status_code==200,response.text
    result=response.json()['execution']['result']
    assert result['mode']=='full' and result['total_items']==6,result
    assert result['status']=='paused' and result['delivery_status']=='preparing' and result['started_at'] is None
    assert result['elapsed_seconds']==0 and result['answer_binding'] is None
    assert '没有开始计时' in response.json()['execution']['response_text']
    assert source['stimulus_text'] not in json.dumps(result,ensure_ascii=False)
    with db.connect() as c:
        assert [tuple(row) for row in c.execute('SELECT plan_item_id,status,started_at,completed_at FROM plan_item ORDER BY plan_item_id')]==tasks
        assert c.execute('SELECT count(*) FROM plan_item_event').fetchone()[0]==events
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0
        step=c.execute('''SELECT s.capability_name,s.status FROM agent_step s JOIN agent_plan p USING(plan_id)
            WHERE p.agent_turn_id=? AND s.step_index=0''',(planned['agent_turn_id'],)).fetchone()
        assert tuple(step)==('exam.start','completed')
    token = client.post('/v1/parent/unlock',json={'pin':'2468'}).json()['parent_token']
    parents = client.get(f'/v1/parent/children/{CHILD}/pet-prompts',headers={'X-HSC-Parent':token}).json()['prompts']
    original, = [item for item in parents if item['prompt_id'].startswith(f"exam-media:{result['run_id']}:")]
    media,_ = bind_audio(client,db,root,sid,original,key='full-original-ready-before-first-resume')
    planner.select('exam.resume','继续模拟卷',arguments={})
    continued = turn(client,sid,'继续模拟卷','full-first-resume-without-status')
    resumed = client.post(f"/v1/agent-turns/{continued['agent_turn_id']}/execute")
    assert resumed.status_code == 200, resumed.text
    actual = resumed.json()['execution']['result']
    assert actual['total_items'] == 6 and actual['status'] == 'active' and actual['started_at']
    with db.connect() as c:
        arrival = c.execute('SELECT created_at,snapshot_json FROM learning_input_arrival WHERE client_action_id=?',
            ('full-first-resume-without-status',)).fetchone()
        frozen = next(item['record'] for item in json.loads(arrival['snapshot_json']) if item.get('kind')=='exam_listening_control')
        assert frozen['delivery_phase'] == 'ready' and frozen['ready_at'] <= arrival['created_at'] <= actual['started_at']
        assert [tuple(row) for row in c.execute('SELECT plan_item_id,status,started_at,completed_at FROM plan_item ORDER BY plan_item_id')]==tasks
        assert c.execute('SELECT count(*) FROM plan_item_event').fetchone()[0]==events
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM pet_media_event WHERE operation='playback'").fetchone()[0]==0


def test_original_heard_answer_reservation_recovers_after_restart_without_second_score(case,monkeypatch):
    from home_study_agent.pet_media_evidence import PetMediaEvidenceService
    from home_study_agent.exam_simulation import ExamSimulationError
    db,sid,service,run,client,planner,prompt,source,mono,root=case
    media,_=bind_audio(client,db,root,sid,prompt,key='reserved-source')
    activate(case)
    media=PetMediaEvidenceService(db,media_root=root).list_for_prompt(sid,prompt_id=prompt['prompt_id'],item_version=prompt['item_version'])['listening'][0]
    play(client,sid,media,mono,'reserved-heard')
    with monkeypatch.context() as interrupted:
        def stop_before_score(*args,**kwargs):
            raise ExamSimulationError('ISOLATED_PRE_SCORE_INTERRUPTION')
        interrupted.setattr(ExamSimulationService,'_submit_reserved',stop_before_score)
        response,planned=answer(case,'original-before-interruption')
        assert response.status_code==422,response.text
    with db.connect() as c:
        slot=c.execute('SELECT * FROM specialized_exam_item WHERE run_id=? AND position=1',(run['run_id'],)).fetchone()
        saved=dict(slot)
        assert slot['answer_text']==source['expected_answer']['exact_answers'][0] and slot['answer_arrived_at']
        assert slot['attempt_id'] is None and slot['authorized_turn_id']==planned['agent_turn_id']
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0
    assert service.recover_after_restart()==1
    activate(case,'fresh-after-reservation-restart')
    recovered=client.post(f"/v1/agent-turns/{planned['agent_turn_id']}/execute")
    assert recovered.status_code==200,recovered.text
    repeated=client.post(f"/v1/agent-turns/{planned['agent_turn_id']}/execute")
    assert repeated.status_code==200 and repeated.json()==recovered.json()
    with db.connect() as c:
        slot=c.execute('SELECT * FROM specialized_exam_item WHERE run_id=? AND position=1',(run['run_id'],)).fetchone()
        assert slot['answer_arrived_at']==saved['answer_arrived_at'] and slot['authorized_turn_id']==saved['authorized_turn_id']
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==1
        assert LearningStateService._attempt(c,slot['attempt_id'])['score_status']=='verified_local'
    assert service.status(run['run_id'])['current_position']==2


@pytest.mark.parametrize('words',['不要开始英语模拟卷','老师说“开始英语模拟卷”','如果开始英语模拟卷会怎样？'])
def test_quote_negative_and_hypothetical_are_not_local_exam_start_grants(case,words):
    db,sid,service,run,client,planner,prompt,source,mono,root=case
    service.cancel(run['run_id'])
    planner.select('exam.start',words,arguments={})
    planned=turn(client,sid,words,'not-an-exam-grant')
    assert planned['action']['type']!='start_exam_simulation',planned
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM specialized_exam_run').fetchone()[0]==1


def test_unreviewed_listening_domain_requires_real_original_before_new_paper(case):
    db,sid,service,run,client,planner,prompt,source,mono,root=case
    service.cancel(run['run_id'])
    with db.connect() as c:
        candidate=c.execute('SELECT source_ref FROM diagnostic_item WHERE diagnostic_item_id=?',(prompt['diagnostic_item_id'],)).fetchone()[0].partition(':')[2]
    token=client.post('/v1/parent/unlock',json={'pin':'2468'}).json()['parent_token']
    withdrawn=client.post(f'/api/parent/children/{CHILD}/curriculum-supply/{candidate}/withdraw',headers={'X-HSC-Parent':token},
        json=dict(action_id='remove-only-audited-original',expected_version=4,note='撤回真实原音题源，不假定播放次数。'))
    assert withdrawn.status_code==200,withdrawn.text
    planner.select('exam.start','开始英语模拟卷',arguments={})
    planned=turn(client,sid,'开始英语模拟卷','need-reviewed-original')
    assert planned['action']['type']=='start_exam_simulation'
    response=client.post(f"/v1/agent-turns/{planned['agent_turn_id']}/execute")
    assert response.status_code==200,response.text
    value=response.json()['execution']
    assert value['result']['reason']=='EXAM_LISTENING_ORIGINAL_INSTRUCTION_REQUIRED'
    assert '原播放要求' in value['response_text'] and '没有开始计时' in value['response_text'] and 'EXAM_' not in value['response_text']
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM specialized_exam_run').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0


def test_cancel_remaining_paper_preserves_saved_original_score_and_rejects_old_media(case):
    from home_study_agent.pet_media_evidence import PetMediaEvidenceService
    db,sid,service,run,client,planner,prompt,source,mono,root=case
    media,_=bind_audio(client,db,root,sid,prompt,key='cancel-history-source')
    activate(case)
    media=PetMediaEvidenceService(db,media_root=root).list_for_prompt(sid,prompt_id=prompt['prompt_id'],item_version=prompt['item_version'])['listening'][0]
    media=play(client,sid,media,mono,'cancel-history-heard')
    response,planned=answer(case,'saved-before-cancel')
    assert response.status_code==200,response.text
    planner.select('exam.cancel','取消模拟卷',arguments={})
    control=turn(client,sid,'取消模拟卷','cancel-only-remaining')
    cancelled=client.post(f"/v1/agent-turns/{control['agent_turn_id']}/execute")
    assert cancelled.status_code==200,cancelled.text
    assert cancelled.json()['execution']['result']['status']=='cancelled'
    assert service.active_for_session(sid) is None
    with db.connect() as c:
        historical=c.execute('SELECT * FROM specialized_exam_run WHERE run_id=?',(run['run_id'],)).fetchone()
        slot=c.execute('SELECT * FROM specialized_exam_item WHERE run_id=? AND position=1',(run['run_id'],)).fetchone()
        assert service._current_attempt_score(c,historical,slot,slot['attempt_id'])==1
        assert LearningStateService._attempt(c,slot['attempt_id'])['score_status']=='verified_local'
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==1
    old=client.post(f'/v1/sessions/{sid}/pet-media/listening/control',json=dict(media_id=media['media_id'],
        generation=media['generation'],expected_revision=media['revision'],command='resume',client_action_id='cancelled-old-resume'))
    assert old.status_code==409


def test_restart_settles_only_exact_persisted_pending_receipt_without_replay_or_score(case,monkeypatch):
    from datetime import timedelta
    from home_study_agent.pet_media_evidence import PetMediaEvidenceService,_now
    db,sid,service,run,client,planner,prompt,source,mono,root=case
    media,_=bind_audio(client,db,root,sid,prompt,key='pending-source')
    activate(case)
    media=PetMediaEvidenceService(db,media_root=root).list_for_prompt(sid,prompt_id=prompt['prompt_id'],item_version=prompt['item_version'])['listening'][0]
    control=client.post(f'/v1/sessions/{sid}/pet-media/listening/control',json=dict(media_id=media['media_id'],
        generation=media['generation'],expected_revision=media['revision'],command='resume',client_action_id='pending-play'))
    assert control.status_code==200,control.text
    media=control.json()['media']
    started=client.post(f'/v1/sessions/{sid}/pet-media/listening/playback',json=dict(media_id=media['media_id'],
        generation=media['generation'],expected_revision=media['revision'],event='started',audio_sha256=media['audio']['sha256'],
        frame_position=0,monotonic_ms=1000,client_action_id='pending-started'))
    assert started.status_code==200,started.text
    media=started.json()['media']
    receipt=dict(media_id=media['media_id'],generation=media['generation'],expected_revision=media['revision'],
        event='ended',audio_sha256=media['audio']['sha256'],frame_position=media['audio']['frames'],
        monotonic_ms=2000,client_action_id='persisted-ended')
    ended=client.post(f'/v1/sessions/{sid}/pet-media/listening/playback',json=receipt)
    assert ended.status_code==202,ended.text
    assert ended.json()['media']['pending_playback_receipt']==receipt
    assert service.recover_after_restart()==1
    original=service.status(run['run_id'])
    future=_now()+timedelta(seconds=2)
    monkeypatch.setattr('home_study_agent.pet_media_evidence._now',lambda:future)
    altered=client.post(f'/v1/sessions/{sid}/pet-media/listening/playback',json={**receipt,'client_action_id':'invented-after-restart'})
    assert altered.status_code==409
    settled=client.post(f'/v1/sessions/{sid}/pet-media/listening/playback',json=receipt)
    assert settled.status_code==200,settled.text
    assert settled.json()['media']['qualification']['task_complete_play_count']==1
    repeated=client.post(f'/v1/sessions/{sid}/pet-media/listening/playback',json=receipt)
    assert repeated.status_code==200,repeated.text
    assert service.status(run['run_id'])['status']=='paused'
    assert service.status(run['run_id'])['started_at']==original['started_at']
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM pet_media_event WHERE operation='playback_settled'").fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM pet_media_event WHERE operation='control'").fetchone()[0]==1


@pytest.fixture(params=['school','pet'])
def group_case(tmp_path,monkeypatch,request):
    from test_listening_v2_original_units import setup,publish
    db,supply,spec,child,sources,excerpts=setup(tmp_path,monkeypatch,request.param)
    from home_study_agent.database import Database
    db=Database(db.path,media_root=tmp_path)
    settings=Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False,agent_runtime_enabled=True)
    mono=[1000]
    monkeypatch.setattr('home_study_agent.pet_media_evidence._clock_ms',lambda:mono[0])
    planner=Gateway()
    with TestClient(create_app(settings,model_gateway=planner),client=('127.0.0.1',42421)) as client:
        headers={'X-HSC-Parent':client.post('/v1/parent/setup',json={'pin':'2468'}).json()['parent_token']}
        published=[publish(client,headers,{**spec,'excerpt_id':eid},child,'exam-group-source-'+str(i)) for i,eid in enumerate(excerpts)]
        with db.connect() as c:
            selected=[dict(c.execute('''SELECT di.diagnostic_item_id,kn.knowledge_node_id,kn.subject,kn.title
                FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id) WHERE di.diagnostic_item_id=?''',
                (row['diagnostic_item_id'],)).fetchone()) for row in published[:2]]
        track='junior_exam' if request.param=='school' else 'pet'
        service=ExamSimulationService(db)
        domain=service.specialized._mastery_domain(track,selected[0])
        monkeypatch.setattr(ExamSimulationService,'_balanced_items',staticmethod(lambda *args:[(domain,selected[0]),(domain,selected[1])]))
        sid=client.post('/v1/sessions',json={'child_id':child,'client_action_id':'group-exam-session'}).json()['session_id']
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,));c.commit()
        run=service.start(sid,track,'英语','practice',2,30,'whole-original-group')
        parents=client.get(f'/v1/parent/children/{child}/pet-prompts',headers=headers).json()['prompts']
        prompt,=parents
        yield (db,sid,service,run,client,planner,prompt,sources[0],mono,tmp_path),published,supply,headers


def test_shared_original_group_scores_two_slots_from_one_actual_service_receipt_set(group_case):
    from home_study_agent.pet_media_evidence import PetMediaEvidenceService
    case,published,supply,headers=group_case
    db,sid,service,run,client,planner,prompt,source,mono,root=case
    from home_study_agent.exam_listening_delivery import complete_group_selection
    from home_study_agent.exam_simulation import ExamSimulationError
    with db.connect() as c:
        actual=[dict(c.execute('''SELECT di.diagnostic_item_id,kn.knowledge_node_id,kn.subject,kn.title
            FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id) WHERE di.diagnostic_item_id=?''',
            (row['diagnostic_item_id'],)).fetchone()) for row in published]
        domain=service.specialized._mastery_domain(run['track'],actual[0])
        selected=[('whole-section',domain,actual[0])]
        by_domain={domain:actual}
        assert [row[2]['diagnostic_item_id'] for row in complete_group_selection(c,prompt['child_id'],selected,by_domain,full=False)]==[row['diagnostic_item_id'] for row in published[:2]]
        with pytest.raises(ExamSimulationError,match='EXAM_LISTENING_GROUP_BLUEPRINT_QUOTA_UNAVAILABLE'):
            complete_group_selection(c,prompt['child_id'],selected,by_domain,full=True)
        with pytest.raises(ExamSimulationError,match='EXAM_MEDIA_SOURCE_GROUP_INCOMPLETE'):
            complete_group_selection(c,prompt['child_id'],selected,{domain:actual[:1]},full=False)
        whole=complete_group_selection(c,prompt['child_id'],selected*2,by_domain,full=True)
        assert len(whole)==2 and {row[2]['diagnostic_item_id'] for row in whole}=={row['diagnostic_item_id'] for row in published[:2]}
    media,_=bind_audio(client,db,root,sid,prompt,key='shared-original')
    active,starting_turn=activate(case)
    media=PetMediaEvidenceService(db,media_root=root).list_for_prompt(sid,prompt_id=prompt['prompt_id'],item_version=prompt['item_version'])['listening'][0]
    play(client,sid,media,mono,'shared-service-callbacks')
    first,first_turn=answer(case,'first-group-answer')
    assert first.status_code==200,first.text
    second_status=service.status(run['run_id'])
    cue=second_status['audio_delivery']
    assert second_status['current_position']==2 and cue['item_id']==published[1]['diagnostic_item_id']
    assert cue['media_item_id']==published[0]['diagnostic_item_id'] and cue['prompt_id']==prompt['prompt_id']
    assert cue['item_version']==cue['media_prompt']['item_version']==prompt['item_version']
    with db.connect() as c:
        second_prompt=c.execute('SELECT item_version FROM diagnostic_prompt WHERE prompt_id=?',(f"exam-media:{run['run_id']}:2",)).fetchone()
        assert second_prompt[0]!=cue['item_version']
    assert cue['resource']['completed_play_count']==cue['resource']['required_play_count']==1
    second,second_turn=answer(case,'second-group-answer')
    assert second.status_code==200,second.text
    final=service.status(run['run_id'])
    assert final['status']=='completed' and final['answered_items']==2 and final['result']['verified_items']==2,final
    cached=client.post(f"/v1/agent-turns/{first_turn['agent_turn_id']}/execute")
    assert cached.status_code==200,cached.text
    replayed=cached.json()['execution']
    assert replayed['replayed'] is True and replayed['recorded_receipt'] is True and replayed['replay_audio_allowed'] is False
    assert replayed['result']==first.json()['execution']['result']
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==2
        assert c.execute('SELECT count(*) FROM pet_media_binding').fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM pet_media_event WHERE operation='playback' AND json_extract(payload_json,'$.event')='ended'").fetchone()[0]==1
        # Normal completion of the daily session cannot revoke historical owner reads.
        c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?",(sid,));c.commit()
    with db.connect() as c:
        historical_run=c.execute('SELECT * FROM specialized_exam_run WHERE run_id=?',(run['run_id'],)).fetchone()
        slots=c.execute('SELECT * FROM specialized_exam_item WHERE run_id=? ORDER BY position',(run['run_id'],)).fetchall()
        assert [service._current_attempt_score(c,historical_run,slot,slot['attempt_id']) for slot in slots]==[1,1]
    supply.withdraw(prompt['child_id'],published[1]['candidate_id'],actor='parent',note='Withdraw the original member authority.',action_id='withdraw-group-member',expected_version=4)
    with db.connect() as c:
        assert [service._current_attempt_score(c,historical_run,slot,slot['attempt_id']) for slot in slots]==[None,None]
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==2


@pytest.mark.parametrize('capability', ['exam.resume', 'exam.submit'])
def test_stale_planned_load_competing_with_completed_typed_step_cannot_replay_audio(group_case, monkeypatch, capability):
    case,published,supply,headers=group_case
    db,sid,service,run,client,planner,prompt,source,mono,root=case
    executor=client.app.state.agent_capability_executor
    real_load=executor._load
    planned_snapshots={}

    def capture_planned(turn_id):
        row=real_load(turn_id)
        if row['status']=='planned' and row['capability_name'] in {'exam.resume','exam.submit'}:
            planned_snapshots[turn_id]=row
        return row

    monkeypatch.setattr(executor, '_load', capture_planned)
    media,_=bind_audio(client,db,root,sid,prompt,key='competing-original')
    active,starting_turn=activate(case,key='competing-explicit-resume')
    chosen=starting_turn
    if capability=='exam.submit':
        from home_study_agent.pet_media_evidence import PetMediaEvidenceService
        media=PetMediaEvidenceService(db,media_root=root).list_for_prompt(
            sid,prompt_id=prompt['prompt_id'],item_version=prompt['item_version'])['listening'][0]
        play(client,sid,media,mono,'competing-service-callbacks')
        first,chosen=answer(case,key='competing-first-answer')
        assert first.status_code==200,first.text
    turn_id=chosen['agent_turn_id']
    stale=planned_snapshots[turn_id]
    assert stale['status']=='planned' and stale['capability_name']==capability
    assert not str(stale['turn_action_id']).startswith('realtime-')

    def durable_snapshot():
        with db.connect() as c:
            step=c.execute('SELECT status,result_json FROM agent_step WHERE step_id=?',(stale['step_id'],)).fetchone()
            run_row=c.execute('SELECT current_position,control_revision,result_json FROM specialized_exam_run WHERE run_id=?',(run['run_id'],)).fetchone()
            attempts=[tuple(row) for row in c.execute('SELECT attempt_id,evidence_id,submitted_answer FROM diagnostic_attempt ORDER BY attempt_id')]
            media_events=[tuple(row) for row in c.execute('SELECT event_id,operation,payload_json FROM pet_media_event ORDER BY event_id')]
        return dict(step=tuple(step),run=tuple(run_row),attempts=attempts,media_events=media_events)

    before=durable_snapshot()
    assert before['step'][0]=='completed'
    saved=json.loads(before['step'][1])
    exam=saved['result'].get('exam') or saved['result']
    assert exam['audio_delivery']['version']=='exam_listening_delivery_v1'
    # Both callers loaded planned; the first has now committed. The second must
    # use the lock-protected completed branch and suppress its persisted cue.
    monkeypatch.setattr(executor, '_load', lambda requested: stale if requested==turn_id else real_load(requested))
    cached=client.post(f'/v1/agent-turns/{turn_id}/execute')
    assert cached.status_code==200,cached.text
    replayed=cached.json()['execution']
    assert replayed.get('replayed') is True
    assert replayed.get('recorded_receipt') is True
    assert replayed.get('replay_audio_allowed') is False
    assert replayed['evidence']==[] and replayed['result']==saved['result']
    assert durable_snapshot()==before
