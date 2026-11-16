"""Real task/resume/answer/review HTTP; synthetic originals, never real skill data."""
from contextlib import contextmanager
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from privacy_restore_operation import restore_operation
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore, CapabilityRegistry, AgentPlanDecision, ObjectiveDraft, PlannedStep
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator
from home_study_agent.repository import StudyRepository
from home_study_agent.model_gateway import ModelResult
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.writing_response_origin import WritingOriginalService
from home_study_agent.writing_rubric import prepare_writing_review
from test_goal_task_prompt import command
from test_pet_task_execution import generated, CHILD
from test_voice_runtime_binding import capture, interpret
from test_writing_response_origin import TEXT

OTHER = 'child-junior-3'
SPOKEN = 'The students are reading books and writing in a quiet library. I like reading story books there after school because it is comfortable. Maybe they will study English together next.'


class SemanticOnly:
    configured = True

    def __init__(self):
        self.requests = []
        self.decision = {}

    async def complete(self, request):
        assert request.request_type == 'voice_interaction', request.request_type
        self.requests.append(request)
        data = json.loads(request.messages[-1]['content'])
        return ModelResult(content=json.dumps({**self.decision, 'quote':data['transcript'], 'confidence':.99}),
            provider='synthetic', model='controlled-semantic-only', latency_ms=1)


def balance(case, client=None, headers=None):
    client, headers = client or case.client, headers or case.headers
    response = client.get(f'/v1/parent/children/{CHILD}/longitudinal-model', headers=headers)
    assert response.status_code == 200, response.text
    return response.json()['payload']['companion_balance']


def facts(db):
    with db.connect() as c:
        return {name:[tuple(r) for r in c.execute('SELECT * FROM '+name)] for name in
            ('diagnostic_attempt','learning_evidence','rubric_assessment','rubric_review_event','mastery_state')}


def writing_task_command(original):
    """Plan the writing goal before the task starts or the child answers."""
    db, sid, pid, _, _ = original
    runtime, key = AgentRuntimeStore(db), str(uuid4())
    cap = CapabilityRegistry().require('task.start', 'study')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.execute("UPDATE plan_item SET status='completed' WHERE plan_item_id!=?", (pid,))
        c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,
            response_text,action_json,tool_status,orchestrator_version,created_at,client_action_id)
            VALUES(?,?,?,'Start the writing task','start_task','',
                '{"type":"start_next_task"}','pending','controlled-local-review',?,?)''',
            (key,CHILD,sid,datetime.now(timezone.utc).isoformat(),key))
        runtime.persist_plan(c,child_id=CHILD,session_id=sid,agent_turn_id=key,client_action_id=key,
            context_manifest={'local_only':True},decision=AgentPlanDecision(
                ObjectiveDraft('new','learning','Write an original response',('Answer independently',),'diagnose'),
                PlannedStep(cap,{},'Begin the scheduled writing task',cap.expected_evidence),'',False,1))
        c.commit()
    return key


def local_assessment(db, sid, answer, *, reuse_task_objective=False):
    """Runtime registers a local review request for the actual HTTP answer."""
    candidate = RubricEvidenceService(db).diagnostic_candidate_input(CHILD, answer['attempt']['evidence_id'])
    runtime, key = AgentRuntimeStore(db), str(uuid4())
    cap = CapabilityRegistry().require('learning.evaluate_response', 'study')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,
            response_text,action_json,tool_status,orchestrator_version,created_at,client_action_id)
            VALUES(?,?,?,'','local_evaluation','','{"local_only":true,"cloud_transmission_allowed":false}',
                'pending','controlled-local-review',?,?)''',
            (key,CHILD,sid,datetime.now(timezone.utc).isoformat(),key))
        # A saved original belongs to the task that preceded it. A newly created
        # post-answer objective cannot acquire a current written-score receipt.
        if reuse_task_objective:
            objective = c.execute('''SELECT * FROM agent_objective
                WHERE child_id=? AND session_id=? AND status='active' ''', (CHILD,sid)).fetchone()
            assert objective and objective['created_at'] <= answer['attempt']['created_at']
        draft = (ObjectiveDraft('none',None,None,(),objective['phase']) if reuse_task_objective else
                 ObjectiveDraft('new','learning','Review this saved answer',('Answer independently',),'diagnose'))
        runtime.persist_plan(c,child_id=CHILD,session_id=sid,agent_turn_id=key,client_action_id=key,
            context_manifest={'local_only':True},decision=AgentPlanDecision(
                draft,
                PlannedStep(cap,candidate['arguments'],'Review the saved original',cap.expected_evidence),'',False,1))
        c.commit()
    return LearningOutcomeEvaluator(db,StudyRepository(db),None,None).prepare_local_evaluation(key)['evaluation']['rubric_assessment']


@contextmanager
def journey(tmp_path, monkeypatch, modality, *, score=3, hint=0, independent=True,
            before_answer=None, before_review=None, review_hint=None, late_review_objective=False,
            native_launch_attestation_key=None):
    original = generated(tmp_path, modality)
    base, sid, pid, _, _ = original
    settings = Settings(data_dir=tmp_path, database_path=base.path, maintenance_enabled=False,
                        data_encryption_key=b'r'*32, model_provider='qwen', model_api_key='synthetic-03504f547a5a2fc55aec',
                        model_name='isolated-model', voice_intent_model_name='isolated-model',
                        native_launch_attestation_key=native_launch_attestation_key)
    db = Database(base.path, media_root=tmp_path, data_encryption_key=b'r'*32)
    gateway = SemanticOnly()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        token = client.post('/v1/parent/setup', json={'pin':'2468'}).json()['parent_token']
        headers = {'X-HSC-Parent':token}
        PrivacyLifecycleService(db,tmp_path).update_preferences(
            {'cloud_model_text_allowed':True,'raw_media_storage_allowed':True})
        start = writing_task_command(original) if modality == 'writing' else command(original, child_id=CHILD)
        result = client.post(f'/v1/agent-turns/{start}/execute')
        assert result.status_code == 200, result.text
        p = result.json()['execution']['result']['diagnostic_prompt']['prompt']
        assert result.json()['execution']['result']['status'] == 'active'
        gateway.decision = {'role':'control','control':'companion.start_activity','activity_type':'movement'}
        text = '能带我活动一小会儿吗？'
        voice = capture(client,settings,monkeypatch,sid,text,p,key='activity-request-voice')
        turn = interpret(client,sid,text,'activity-request',voice)
        assert turn['action']['type'] == 'start_companion_activity', json.dumps({k:turn.get(k) for k in ('intent','response_text','action')},ensure_ascii=False)
        started = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert started.status_code == 200, started.text
        assert client.post(f'/v1/sessions/{sid}/companion-activities/end',json={'reason':'turn_limit'}).status_code == 200
        gateway.decision = {'role':'control','control':'companion.activity_feedback',
                            'activity_feedback':{'operation':'request_resume'}}
        text = '我准备好继续刚才的学习了。'
        voice = capture(client,settings,monkeypatch,sid,text,p,key='resume-request-voice')
        turn = interpret(client,sid,text,'resume-request',voice)
        resumed = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert resumed.status_code == 200, resumed.text
        case = SimpleNamespace(db=db,sid=sid,pid=pid,prompt=p,settings=settings,gateway=gateway,
                               client=client,headers=headers,modality=modality)
        assert balance(case)['task_resume_receipt_sessions'] == 1
        assert balance(case)['verified_child_return_sessions'] == 0
        if before_answer:
            before_answer(case)
        text = TEXT if modality == 'writing' else SPOKEN
        payload = dict(prompt_id=p['prompt_id'],item_version=p['item_version'],answer=text,
                       hint_level=hint,client_action_id='original-response')
        if modality == 'writing':
            original = WritingOriginalService(db).capture_native_response(sid,prompt_id=p['prompt_id'],
                item_version=p['item_version'],text=text,client_action_id='native-original')
            payload['writing_original_artifact_id'] = original['artifact_id']
        else:
            gateway.decision = {'role':'answer','control':None}
            voice = capture(client,settings,monkeypatch,sid,text,p,key='original-speaking-voice')
            turn = interpret(client,sid,text,'original-speaking-answer',voice)
            if turn['intent'] == 'pending_answer_clarification':
                assert client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute").status_code == 200
                confirmed = client.post(f'/v1/sessions/{sid}/agent-turn',json={
                    'transcript':'刚才那句就是我的答案','client_action_id':'confirm-speaking-answer'})
                assert confirmed.status_code == 200, confirmed.text
                turn = confirmed.json()
            assert turn['action']['source_voice_input_id'] == voice
            payload['authorized_turn_id'] = turn['agent_turn_id']
            case.voice = voice
        answer = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer',json=payload)
        assert answer.status_code == 200, answer.text
        assert answer.json()['status'] == 'answered'
        case.answer = answer.json()
        case.answer_payload = payload
        if before_review:
            before_review(case)
        assessment = (local_assessment(db,sid,answer.json(),reuse_task_objective=not late_review_objective)
            if modality == 'writing' else local_assessment(db,sid,answer.json()))
        service = RubricEvidenceService(db)
        body = dict(expected_revision=0,client_action_id='original-human-review',operation='approve',
            reason='Synthetic original review, not a real child assessment',independent=independent,
            hint_level=hint if review_hint is None else review_hint,
            criterion_evidence=[dict(criterion=k,response_quote=text) for k in assessment['criteria']])
        if modality == 'writing':
            with db.connect() as c:
                binding = prepare_writing_review(c,CHILD,assessment['assessment_id'])
            body.update(review_profile=binding['profile'],writing_evidence={
                'binding_version':binding['binding_version'],
                'task_point_evidence':[dict(task_point=k,response_quote=text) for k in binding['task_points']]})
            dimensions = assessment['dimensions']
        else:
            context = service.audio_context(CHILD,assessment['assessment_id'])
            assert len(context['bindings']) == 1, context
            binding = context['bindings'][0]
            body.update(review_profile='pet_speaking_audio_v1',audio_evidence={
                'media_id':binding['media_id'], 'review':{'schema':'pet_speaking_audio_references_v1',
                    'binding_version':binding['binding_version'],'audio_sha256':binding['audio']['sha256'],
                    'observations':[dict(dimension_id=k,start_frame=0,end_frame=binding['audio']['frames'],
                        heard_text='Synthetic waveform fixture, not real speech quality',rationale='Isolated frame annotation')
                        for k in binding['dimensions_required']]}})
            dimensions = context['dimensions']
        body['dimensions'] = [dict(dimension_id=d['dimension_id'],score=score,response_quotes=[text]
            if d['dimension_id'] in {'task','coherence','language'} else [], rationale='Synthetic original dimension review') for d in dimensions]
        case.assessment, case.body = assessment, body
        case.review_route = f"/v1/parent/children/{CHILD}/learning-rubrics/{assessment['assessment_id']}/review"
        assert balance(case)['verified_child_return_sessions'] == 0
        mastery_before_review = facts(db)['mastery_state']
        reviewed = client.post(case.review_route,headers=headers,json=body)
        assert reviewed.status_code == 200, reviewed.text
        case.review = reviewed.json()
        if modality == 'writing':
            assert facts(db)['mastery_state'] == mastery_before_review
        yield case


@pytest.mark.parametrize('modality',['writing','speaking'])
def test_real_original_review_reaches_parent_return_without_new_learning_facts(tmp_path,monkeypatch,modality):
    with journey(tmp_path,monkeypatch,modality) as case:
        before = facts(case.db)
        calls = len(case.gateway.requests)
        view = balance(case)
        assert view['verified_child_return_sessions'] == 1, view
        assert view['task_resume_receipt_sessions'] == 1
        assert view['habit_success_sessions'] == 0
        proof = view['outcome_evidence'][0]['return_verification']
        assert proof['independent'] is True
        assert proof['evidence_refs'] == [case.answer['attempt']['evidence_id']]
        assert facts(case.db) == before
        assert len(case.gateway.requests) == calls
        if modality == 'writing':
            from home_study_agent.writing_score import current_written_score
            with case.db.connect() as c:
                score = current_written_score(c,CHILD,case.assessment['assessment_id'],now=datetime.now(timezone.utc))
                origin = c.execute('''SELECT objective_id FROM agent_plan WHERE child_id=? AND session_id=?
                    AND json_extract(context_manifest_json,'$.goal_prompt_binding.prompt_id')=?
                    ORDER BY created_at LIMIT 1''', (CHILD,case.sid,case.prompt['prompt_id'])).fetchone()
                objective = c.execute('SELECT * FROM agent_objective WHERE objective_id=?',
                    (score['current']['objective_id'],)).fetchone()
                assert objective['created_at'] <= case.answer['attempt']['created_at']
                assert origin['objective_id'] == objective['objective_id']
                assert c.execute("SELECT COUNT(*) FROM agent_objective WHERE child_id=? AND objective_type='learning'",
                    (CHILD,)).fetchone()[0] == 1
            assert score['decision']['authority_id'] == case.review['event']['event_id']
            assert score['current']['source_evidence_id'] == case.answer['attempt']['evidence_id']
            assert score['receipt']['independent'] is True
            assert score['mastery_claim_allowed'] is False


def update(db, sql, params=()):
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute(sql, params)
        c.commit()


def unlock(client):
    response = client.post('/v1/parent/unlock',json={'pin':'2468'})
    assert response.status_code == 200, response.text
    return {'X-HSC-Parent':response.json()['parent_token']}


@pytest.mark.parametrize('modality',['writing','speaking'])
def test_wrong_but_independent_original_is_return_not_mastery_and_survives_restart(tmp_path,monkeypatch,modality):
    def pause_after_answer(case):
        StudyRepository(case.db).pause_plan_item(case.pid,'pause-after-real-answer')
    with journey(tmp_path,monkeypatch,modality,score=0,before_review=pause_after_answer) as case:
        assert case.review['assessment']['review']['passed'] is False
        before = facts(case.db)
        view = balance(case)
        assert view['verified_child_return_sessions'] == 1
        assert view['habit_success_sessions'] == 0
        episode = view['outcome_evidence'][0]['return_verification']['episodes'][0]
        assert episode['closed_at']
        assert episode['evidence'][0]['occurred_at'] == case.answer['attempt']['created_at']
        assert 'qualification_basis' in episode['evidence'][0]
        assert case.review['qualification']['eligible_for_mastery_gate'] is False
        assert facts(case.db) == before
    with TestClient(create_app(case.settings,model_gateway=case.gateway)) as client:
        headers = unlock(client)
        assert balance(case,client,headers)['verified_child_return_sessions'] == 1
        replay = client.post(f'/v1/sessions/{case.sid}/diagnostic-prompt/answer',json=case.answer_payload)
        assert replay.status_code == 200
        assert {k:v for k,v in replay.json()['attempt'].items() if k!='learning_adaptation'} == {
            k:v for k,v in case.answer['attempt'].items() if k!='learning_adaptation'}
        again = client.post(case.review_route,headers=headers,json=case.body)
        assert again.status_code == 200 and again.json()['event'] == case.review['event']
        assert facts(case.db) == before


@pytest.mark.parametrize('modality',['writing','speaking'])
@pytest.mark.parametrize('condition',['hint','assisted','closed_segment'])
def test_reviewed_score_cannot_replace_independence_or_original_answer_window(tmp_path,monkeypatch,modality,condition):
    def before(case):
        if condition == 'closed_segment':
            StudyRepository(case.db).pause_plan_item(case.pid,'pause-before-answer')
    with journey(tmp_path,monkeypatch,modality,hint=1 if condition=='hint' else 0,
                 independent=condition!='assisted',before_answer=before,review_hint=0) as case:
        view = balance(case)
        assert view['verified_child_return_sessions'] == 0, view
        assert view['task_resume_receipt_sessions'] == 1
        assert view['habit_success_sessions'] == 0


@pytest.mark.parametrize('modality',['writing','speaking'])
def test_current_review_retraction_changes_parent_projection_and_old_key_cannot_restore(tmp_path,monkeypatch,modality):
    with journey(tmp_path,monkeypatch,modality) as case:
        current = case.client.get(case.review_route.removesuffix('/review'),headers=case.headers).json()
        assert current['assessment']['source_status'] == 'current'
        assert current['assessment']['review']['operation'] == 'approve'
        assert balance(case)['verified_child_return_sessions'] == 1
        wrong_child = case.client.post(case.review_route.replace(CHILD,OTHER),headers=case.headers,
            json={**case.body,'client_action_id':'other-child-review'})
        assert wrong_child.status_code == 409
        assert wrong_child.json()['detail']['code'] == 'RUBRIC_ASSESSMENT_NOT_OWNED'
        other = case.client.get(f'/v1/parent/children/{OTHER}/longitudinal-model',headers=case.headers)
        assert other.status_code == 200 and other.json()['payload']['companion_balance']['verified_child_return_sessions'] == 0
        removed = case.client.post(case.review_route,headers=case.headers,json=dict(
            operation='retract',expected_revision=1,client_action_id='retract-original-review',reason='Original human review withdrawn'))
        assert removed.status_code == 200, removed.text
        before = facts(case.db)
        assert balance(case)['verified_child_return_sessions'] == 0
    with TestClient(create_app(case.settings,model_gateway=case.gateway)) as client:
        headers = unlock(client)
        replay = client.post(case.review_route,headers=headers,json=case.body)
        assert replay.status_code == 200 and replay.json()['event'] == case.review['event']
        view = balance(case,client,headers)
        assert view['verified_child_return_sessions'] == view['habit_success_sessions'] == 0
        assert view['task_resume_receipt_sessions'] == 1
        assert facts(case.db) == before


@pytest.mark.parametrize('modality',['writing','speaking'])
def test_real_backup_restore_quarantines_voice_authority_not_reapproves_history(tmp_path,monkeypatch,modality):
    from synthetic_native_restore_request import signed_restore_post, KEY

    with journey(tmp_path,monkeypatch,modality,native_launch_attestation_key=KEY) as case:
        assert balance(case)['verified_child_return_sessions'] == 1
        completed = case.client.post(f'/v1/plan-items/{case.pid}/complete',json={
            'actual_minutes':1,'client_action_id':'complete-actual-original',
            'completion_note':'Original response and current explicit review recorded'})
        assert completed.status_code == 200, completed.text
        for current, target in [('study','reflection'),('reflection','evolving'),('evolving','completed')]:
            response = case.client.post(f'/v1/sessions/{case.sid}/transition',json={
                'expected_state':current,'target_state':target,'reason':'Synthetic closed study before parent restore',
                'client_action_id':'finish-before-restore-'+target})
            assert response.status_code == 200, response.text
        assert balance(case)['verified_child_return_sessions'] == 1
        before = facts(case.db)
        backup = case.client.post('/v1/parent/backups',headers=case.headers)
        assert backup.status_code == 200, backup.text
        restored = signed_restore_post(case.client,headers=case.headers,json={
            'backup_id':backup.json()['backup_id'],'confirmation':'确认恢复',
            'operation':restore_operation(case.client,headers=case.headers)})
        assert restored.status_code == 200, restored.text
        after = balance(case,headers=unlock(case.client))
        # Restore quarantines audio/control permission, not valid historical
        # native written originals or completed task-resume facts.
        expected = 1 if modality == 'writing' else 0
        assert after['verified_child_return_sessions'] == expected
        assert after['habit_success_sessions'] == 0
        with case.db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM voice_capture_binding WHERE status!='cancelled'").fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM voice_control_intent_request WHERE status!='cancelled'").fetchone()[0] == 0
        assert facts(case.db)['rubric_review_event'] == before['rubric_review_event']
        assert facts(case.db)['diagnostic_attempt'] == before['diagnostic_attempt']
    with TestClient(create_app(case.settings,model_gateway=case.gateway)) as client:
        assert balance(case,client,unlock(client))['verified_child_return_sessions'] == expected
        assert facts(case.db)['rubric_review_event'] == before['rubric_review_event']


@pytest.mark.parametrize('modality',['writing','speaking'])
@pytest.mark.parametrize('fault',['item_version','original','withdrawn','unbound_task'])
def test_current_original_and_exact_task_are_rechecked_not_cached(tmp_path,monkeypatch,modality,fault):
    with journey(tmp_path,monkeypatch,modality) as case:
        assert balance(case)['verified_child_return_sessions'] == 1
        if fault == 'item_version':
            update(case.db,"UPDATE diagnostic_item SET prompt=prompt||' changed' WHERE diagnostic_item_id=?",(case.prompt['diagnostic_item_id'],))
        elif fault == 'original':
            if modality == 'speaking':
                cancelled = case.client.post(f'/v1/sessions/{case.sid}/asr/actions/original-speaking-voice/cancel')
                assert cancelled.status_code == 200, cancelled.text
            else:
                update(case.db,"UPDATE capture_artifact SET status='deleted' WHERE artifact_id=?",(case.answer_payload['writing_original_artifact_id'],))
        elif fault == 'withdrawn':
            from home_study_agent.privacy_source_withdrawal import ensure_schema, record
            with case.db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                ensure_schema(c)
                record(c,CHILD,[case.prompt['diagnostic_item_id']],[case.answer['attempt']['evidence_id']])
                c.commit()
        else:
            update(case.db,"UPDATE agent_plan SET context_manifest_json=json_remove(context_manifest_json,'$.goal_prompt_binding') WHERE child_id=?",(CHILD,))
        before = facts(case.db)
        view = balance(case)
        assert view['verified_child_return_sessions'] == 0, view
        assert view['habit_success_sessions'] == 0
        assert facts(case.db) == before
    with TestClient(create_app(case.settings,model_gateway=case.gateway)) as client:
        view = balance(case,client,unlock(client))
        assert view['verified_child_return_sessions'] == 0
        assert facts(case.db)['diagnostic_attempt'] == before['diagnostic_attempt']


@pytest.mark.parametrize('modality',['writing','speaking'])
def test_exact_tutor_history_before_original_answer_is_not_independent_return(tmp_path,monkeypatch,modality):
    from home_study_agent.teaching_question_target import resolve_question_target
    with journey(tmp_path,monkeypatch,modality) as case:
        assert balance(case)['verified_child_return_sessions'] == 1
        with case.db.connect() as c:
            target = resolve_question_target(c,case.sid,subject='英语',question=case.prompt['item']['prompt'],
                prompt_id=case.prompt['prompt_id'],item_version=case.prompt['item_version'])
        assert target['status'] == 'resolved', target
        saved = StudyRepository(case.db).save_tutor_turn(case.sid,case.pid,'英语',case.prompt['item']['prompt'],
            'Synthetic exact original hint.', 'local','synthetic','exact-tutor-history',hint_level=1,
            output_envelope={'local_teaching_control':{'question_target':target}})
        # Recovered exact history arrived after review; its original time is
        # before the answer. Never change the original answer or review score.
        at = (datetime.fromisoformat(case.answer['attempt']['created_at'])-timedelta(microseconds=1)).isoformat()
        update(case.db,'UPDATE tutor_turn SET created_at=? WHERE turn_id=?',(at,saved['turn_id']))
        before = facts(case.db)
        assert balance(case)['verified_child_return_sessions'] == 0
        assert facts(case.db) == before


def test_writing_late_objective_review_cannot_become_return_authority(tmp_path,monkeypatch):
    from home_study_agent.writing_score import current_written_score
    with journey(tmp_path,monkeypatch,'writing',late_review_objective=True) as case:
        assert case.review['assessment']['review']['operation'] == 'approve'
        assert case.review['qualification']['score_qualified'] is False
        with case.db.connect() as c:
            with pytest.raises(ValueError,match='SCORE_RECEIPT_UNAVAILABLE'):
                current_written_score(c,CHILD,case.assessment['assessment_id'],now=datetime.now(timezone.utc))
        before = facts(case.db)
        assert balance(case)['verified_child_return_sessions'] == 0
        assert facts(case.db) == before
    with TestClient(create_app(case.settings,model_gateway=case.gateway)) as client:
        assert balance(case,client,unlock(client))['verified_child_return_sessions'] == 0


@pytest.mark.parametrize('owner',['child_id','session_id'])
def test_writing_original_ownership_rechecked_after_review(tmp_path,monkeypatch,owner):
    with journey(tmp_path,monkeypatch,'writing') as case:
        assert balance(case)['verified_child_return_sessions'] == 1
        replacement = OTHER if owner == 'child_id' else 'not-the-original-session'
        if owner == 'session_id':
            # Keep the foreign key valid while corrupting exact session ownership.
            update(case.db,'''INSERT INTO daily_session(session_id,child_id,status,state,
                started_at,last_checkpoint_at,client_action_id)
                SELECT ?,child_id,'completed','completed',started_at,last_checkpoint_at,
                    'unrelated-owner-session' FROM daily_session WHERE session_id=?''', (replacement,case.sid))
        update(case.db,f'UPDATE capture_artifact SET {owner}=? WHERE artifact_id=?',
            (replacement,case.answer_payload['writing_original_artifact_id']))
        before = facts(case.db)
        assert balance(case)['verified_child_return_sessions'] == 0
        assert facts(case.db) == before
    with TestClient(create_app(case.settings,model_gateway=case.gateway)) as client:
        assert balance(case,client,unlock(client))['verified_child_return_sessions'] == 0
