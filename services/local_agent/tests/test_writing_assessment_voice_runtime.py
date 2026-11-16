"""Real stored assessment/review and runtime plans; no live model or child data."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.repository import StudyRepository
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_agent_continuation import orchestrator_for
from test_writing_response_origin import writing, CHILD, TEXT
from test_writing_teaching import ready
from test_voice_runtime_binding import capture
from test_writing_voice_runtime_http import page, readback_receipt


@pytest.fixture
def feedback(writing, tmp_path, request):
    db, sid, _, prompt, _ = writing
    if getattr(request, 'param', None) == 'model':
        from test_writing_teaching import test_real_evaluator_candidate_can_teach_without_parent_review_but_not_certify
        from home_study_agent.writing_teaching import WritingTeachingService
        test_real_evaluator_candidate_can_teach_without_parent_review_but_not_certify(writing)
        service = WritingTeachingService(db)
        assessment = service.read(sid, prompt_id=prompt['prompt_id'],item_version=prompt['item_version'])
    else:
        service, rubric, assessment = ready(writing)
    PrivacyLifecycleService(db, tmp_path).initialize()
    agent, gateway = orchestrator_for(db, tmp_path, {})
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        saved = c.execute('''SELECT p.agent_turn_id,e.result_json FROM learning_evaluation_execution e
            JOIN agent_step s USING(step_id) JOIN agent_plan p USING(plan_id)''').fetchone()
        c.commit()
    result = json.loads(saved['result_json'])
    agent.complete_turn(saved['agent_turn_id'], result['response_text'], 'completed', None, result)
    return writing, agent, gateway, assessment, saved['agent_turn_id']


@pytest.mark.parametrize('kind,quote,cap,action', [
    ('revision', '我想按刚才的反馈自己改一遍', 'writing.revise_original', 'revise_writing_original'),
    ('independent_check', '再来一道不一样的，我自己写', 'writing.independent_check', 'check_writing_independently'),
])
def test_reviewed_assessment_natural_request_freezes_original_source_before_dispatch(feedback, kind, quote, cap, action):
    writing, agent, gateway, assessment, feedback_turn = feedback
    db, sid, _, parent, _ = writing
    gateway.output = {'role': 'control', 'control': cap, 'quote': quote, 'confidence': .99}
    heard = asyncio.run(agent.interpret(sid, quote, 'followup-request', True))
    with db.connect() as c:
        snapshot = c.execute("SELECT snapshot_json FROM learning_input_arrival WHERE client_action_id='followup-request'").fetchone()[0]
        states = [dict(r) for r in c.execute('''SELECT t.tool_status,s.status,e.state,e.record_json FROM agent_turn t
            JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
            JOIN learning_evaluation_execution e USING(step_id)''')]
    frozen = next(x['record'] for x in json.loads(snapshot) if x.get('kind') == 'writing_followup_arrival')
    assert frozen is not None, [(json.loads(x['record_json']).get('source_evidence_id'), x['status']) for x in states]
    assert heard.get('runtime_capability') == cap, (heard, frozen)
    assert heard['action']['type'] == action
    packet = heard['action']['writing_followup_binding']
    assert packet['action']['parent_prompt_id'] == parent['prompt_id']
    assert packet['action']['assessment_id'] == assessment['assessment_id']
    assert packet['action']['kind'] == kind
    assert packet['action']['required_hint_level'] == (1 if kind == 'revision' else 0)
    assert not packet['action']['mastery_claim_allowed']
    with db.connect() as c:
        inspected = AgentRuntimeStore.inspect_writing_followup_execution(c, heard['agent_turn_id'], allow_planned=True)
        assert inspected['binding'] == packet
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
    assert len(gateway.requests) == 1
    messages = json.dumps(gateway.requests[0].messages, ensure_ascii=False)
    for private in (TEXT, parent['prompt_id'], parent['item_version'], assessment['assessment_id'], feedback_turn):
        assert private not in messages
    executed = AgentCapabilityExecutor(db, StudyRepository(db)).execute(heard['agent_turn_id'])
    assert executed['result']['status'] == 'waiting_answer', executed
    next_prompt = executed['result']['prompt']
    assert next_prompt['prompt_id'] != parent['prompt_id']
    agent.complete_turn(heard['agent_turn_id'],executed['response_text'],'completed',None,executed)
    with db.connect() as c:
        saved = c.execute('SELECT objective_id,plan_json,status FROM agent_plan WHERE agent_turn_id=?', (heard['agent_turn_id'],)).fetchone()
        bound = json.loads(saved['plan_json'])['local_learning_binding']
        assert bound['prompt_id'] == next_prompt['prompt_id']
        assert bound['assessment_id'] == assessment['assessment_id']
        assert bound['required_hint_level'] == (1 if kind == 'revision' else 0)
        assert saved['status'] == 'waiting_child'


@pytest.mark.parametrize('feedback', ['model'], indirect=True)
def test_unreviewed_real_formative_http_can_request_independent_writing(feedback, monkeypatch):
    test_actual_asr_http_followup_dispatch_restarts_with_same_prompt(feedback, monkeypatch,
        'independent_check','再来一道不一样的，我自己写','writing.independent_check')
    with feedback[0][0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0


def test_terminal_original_goal_creates_one_distinct_practice_goal(feedback, monkeypatch):
    writing, agent, gateway, _, _ = feedback
    db, sid, _, _, _ = writing
    with db.connect() as c:
        original = c.execute("SELECT objective_id FROM agent_objective WHERE status='active'").fetchone()[0]
        # State-policy unit fixture, not a claim of retained writing mastery.
        c.execute("UPDATE agent_objective SET status='completed',completed_at=updated_at WHERE objective_id=?", (original,))
        c.commit()
    # Mastery reconciliation has separate real-gate tests; do not manufacture
    # retained evidence merely to exercise the new-objective state transition.
    monkeypatch.setattr(agent.agent_runtime,'reconcile_learning_state',lambda *a, **kw: {})
    quote = '再来一道不一样的，我自己写'
    gateway.output = {'role':'control','control':'writing.independent_check','quote':quote,'confidence':.99}
    heard = asyncio.run(agent.interpret(sid,quote,'terminal-practice',True))
    assert heard.get('runtime_capability') == 'writing.independent_check', heard
    with db.connect() as c:
        current = c.execute("SELECT objective_id FROM agent_objective WHERE status='active'").fetchone()[0]
        assert current != original
        assert c.execute('SELECT status FROM agent_objective WHERE objective_id=?',(original,)).fetchone()[0] == 'completed'
    replay = asyncio.run(agent.interpret(sid,quote,'terminal-practice',True))
    assert replay['agent_turn_id'] == heard['agent_turn_id']
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM agent_objective').fetchone()[0] == 2
    executed = AgentCapabilityExecutor(db,StudyRepository(db)).execute(heard['agent_turn_id'])
    agent.complete_turn(heard['agent_turn_id'],executed['response_text'],'completed',None,executed)
    with db.connect() as c:
        plan = c.execute('SELECT objective_id,plan_json FROM agent_plan WHERE agent_turn_id=?', (heard['agent_turn_id'],)).fetchone()
        binding = json.loads(plan['plan_json'])['local_learning_binding']
        assert plan['objective_id'] == current and binding['source_objective_id'] == original


@pytest.mark.parametrize('mutation', ['source', 'review', 'quote', 'journal', 'child', 'paused', 'cancelled', 'new_utterance'])
def test_followup_actual_dispatch_rejects_changed_authority_without_creating_prompt(feedback, mutation):
    writing, agent, gateway, assessment, _ = feedback
    db, sid, _, _, _ = writing
    quote = '我想按刚才的反馈自己改一遍'
    gateway.output = {'role':'control','control':'writing.revise_original','quote':quote,'confidence':.99}
    heard = asyncio.run(agent.interpret(sid,quote,'guarded-followup',True))
    if mutation == 'review':
        from home_study_agent.learning_rubric import RubricEvidenceService
        RubricEvidenceService(db).review(CHILD,assessment['assessment_id'],expected_revision=1,
            client_action_id='retract-feedback',reviewer_id='parent',reviewer_role='parent',operation='retract',reason='撤回合成反馈')
    elif mutation == 'new_utterance':
        asyncio.run(agent.interpret(sid,'别讲了','interrupt-followup',False))
    else:
        with db.connect() as c:
            if mutation == 'source': c.execute("UPDATE capture_artifact SET status='withdrawn' WHERE artifact_type='writing_original'")
            elif mutation == 'quote': c.execute("UPDATE agent_turn SET transcript='不是我的请求' WHERE agent_turn_id=?",(heard['agent_turn_id'],))
            elif mutation == 'journal': c.execute("UPDATE voice_control_intent_request SET status='cancelled'")
            elif mutation == 'child': c.execute("UPDATE agent_turn SET child_id='child-junior-3' WHERE agent_turn_id=?",(heard['agent_turn_id'],))
            else: c.execute('UPDATE agent_objective SET status=? WHERE status=\'active\'',(mutation,))
            c.commit()
    if mutation in {'source','review'}:
        result = AgentCapabilityExecutor(db,StudyRepository(db)).execute(heard['agent_turn_id'])
        assert result['result']['status'] == 'blocked' and not result.get('evidence'), result
        agent.complete_turn(heard['agent_turn_id'],result['response_text'],'completed',None,result)
    else:
        with pytest.raises((InvalidAgentPlanError, ValueError)):
            AgentCapabilityExecutor(db,StudyRepository(db)).execute(heard['agent_turn_id'])
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM writing_followup').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM agent_objective WHERE status='completed'").fetchone()[0] == 0


def test_actual_http_cancelled_followup_cannot_recover_as_new_work(feedback):
    writing, _, gateway, _, _ = feedback
    db, sid, _, _, _ = writing
    settings = Settings(data_dir=db.path.parent,database_path=db.path,maintenance_enabled=False)
    quote = '再来一道不一样的，我自己写'
    gateway.output = {'role':'control','control':'writing.independent_check','quote':quote,'confidence':.99}
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        response = client.post(f'/v1/sessions/{sid}/agent-turn',json={'transcript':quote,'client_action_id':'cancel-work','cloud_text_allowed':True})
        assert response.status_code == 200, response.text
        turn = response.json()
        assert turn.get('runtime_capability') == 'writing.independent_check', turn
        stopped = client.post(f'/v1/sessions/{sid}/agent-turn',json={'transcript':'别讲了','client_action_id':'cancel-now','cloud_text_allowed':False})
        assert stopped.status_code == 200, stopped.text
        result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert result.status_code == 409, result.text
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert result.status_code == 409, result.text
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM writing_followup').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1


def test_actual_http_cancel_after_prompt_creation_invalidates_only_unanswered_followup(feedback):
    writing, _, gateway, _, _ = feedback
    db, sid, _, parent, _ = writing
    settings = Settings(data_dir=db.path.parent,database_path=db.path,maintenance_enabled=False)
    quote = '再来一道不一样的，我自己写'
    gateway.output = {'role':'control','control':'writing.independent_check','quote':quote,'confidence':.99}
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        heard = client.post(f'/v1/sessions/{sid}/agent-turn',json={'transcript':quote,'client_action_id':'later-cancel','cloud_text_allowed':True})
        assert heard.status_code == 200, heard.text
        executed = client.post(f"/v1/agent-turns/{heard.json()['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
        prompt = executed.json()['execution']['result']['prompt']
        stopped = client.post(f'/v1/sessions/{sid}/agent-turn',json={'transcript':'别讲了','client_action_id':'cancel-open-prompt','cloud_text_allowed':False})
        assert stopped.status_code == 200, stopped.text
        from test_diagnostic_prompt_http import answer
        late = answer(client,sid,prompt,answer=TEXT,client_action_id='late-cancelled-followup')
        assert late.status_code == 200 and late.json()['status'] == 'invalidated', late.text
        read = client.get(f'/v1/sessions/{sid}/diagnostic-prompt',params={'prompt_id':prompt['prompt_id']})
        assert read.status_code == 200, read.text
        assert read.json()['status'] == 'invalidated', read.json()
        with db.connect() as c:
            assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?',(parent['prompt_id'],)).fetchone()[0] == 'answered'
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1


def test_saved_followup_source_guard_is_readonly_and_rechecks_original_metadata(feedback):
    writing, agent, gateway, _, _ = feedback
    db, sid, _, parent, _ = writing
    quote = '再来一道不一样的，我自己写'
    gateway.output = {'role':'control','control':'writing.independent_check','quote':quote,'confidence':.99}
    turn = asyncio.run(agent.interpret(sid,quote,'readonly-followup',True))
    result = AgentCapabilityExecutor(db,StudyRepository(db)).execute(turn['agent_turn_id'])
    agent.complete_turn(turn['agent_turn_id'],result['response_text'],'completed',None,result)
    with db.connect() as c:
        prompt = dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',
            (result['result']['prompt']['prompt_id'],)).fetchone())
        answered = dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',
            (parent['prompt_id'],)).fetchone())
        assert AgentRuntimeStore.writing_followup_prompt_source_reason(c,prompt) is None
        mutations = [
            ("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", 'writing_followup_command_cancelled'),
            ("UPDATE agent_step SET status='cancelled' WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", 'writing_followup_command_cancelled'),
            ("UPDATE agent_objective SET status='paused' WHERE objective_id=(SELECT objective_id FROM agent_plan WHERE agent_turn_id=?)", 'writing_followup_goal_changed'),
            ("UPDATE agent_plan SET objective_id=NULL WHERE agent_turn_id=?", 'writing_followup_goal_changed'),
            ("UPDATE agent_turn SET transcript='不是这次请求' WHERE agent_turn_id=?", 'writing_followup_request_changed'),
            ("UPDATE agent_plan SET context_manifest_json=json_remove(context_manifest_json,'$.writing_voice_binding') WHERE agent_turn_id=?", 'writing_followup_binding_changed'),
            ("UPDATE agent_plan SET plan_json=json_set(plan_json,'$.local_learning_binding.prompt_id','not-this-prompt') WHERE agent_turn_id=?", 'writing_followup_binding_changed'),
        ]
        for sql, reason in mutations:
            c.execute('SAVEPOINT changed_source')
            c.execute(sql,(turn['agent_turn_id'],))
            changes = c.total_changes
            for state in ('waiting_answer','answer_saved'):
                assert AgentRuntimeStore.writing_followup_prompt_source_reason(c,{**prompt,'status':state}) == reason
            assert AgentRuntimeStore.writing_followup_prompt_source_reason(c,answered) is None
            assert c.total_changes == changes
            c.execute('ROLLBACK TO changed_source')
            c.execute('RELEASE changed_source')
        for request in ({}, {'writing_authorized_turn_id':[]}, {'writing_authorized_turn_id':turn['agent_turn_id'],'writing_followup':{}}):
            assert AgentRuntimeStore.writing_followup_prompt_source_reason(c,{**prompt,'request_json':json.dumps(request)})
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1


def test_actual_new_revision_manuscript_not_confirmation_reaches_original_goal_feedback(feedback, monkeypatch):
    writing, _, gateway, _, _ = feedback
    db, sid, _, parent, _ = writing
    settings = Settings(data_dir=db.path.parent,database_path=db.path,maintenance_enabled=False)
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        def say(quote, key, prompt, control):
            gateway.output = {'role':'control','control':control,'quote':quote,'confidence':.99}
            voice = capture(client,settings,monkeypatch,sid,quote,prompt,key+'-voice')
            result = client.post(f'/v1/sessions/{sid}/agent-turn',json={
                'transcript':quote,'client_action_id':key,'voice_input_id':voice,'cloud_text_allowed':True})
            assert result.status_code == 200, result.text
            return result.json()
        chosen = say('我想按刚才的反馈自己改一遍','choose-revision',parent,'writing.revise_original')
        prepared = client.post(f"/v1/agent-turns/{chosen['agent_turn_id']}/execute")
        assert prepared.status_code == 200, prepared.text
        prompt = prepared.json()['execution']['result']['prompt']
        manuscript = 'Dear Sam, please meet me at the library on Sunday at nine. We will practise English reading together. Bring your workbook and a pencil. I will bring some stories to share. See you soon, Mia.'
        page(db,sid,manuscript,'new-revised-manuscript')
        say('新稿写完了，帮我核对原稿','look-revision',prompt,'writing.read_original')
        reading = say('这页只有我写的答案','scope-revision',prompt,'writing.read_original')
        assert reading['action']['type'] == 'read_writing_original'
        receipt = readback_receipt(client,sid,reading)
        read = client.put(f"/v1/agent-turns/{reading['agent_turn_id']}/outcome",json={
            'final_response':'原稿已读回','tool_status':'completed','result':receipt})
        assert read.status_code == 200, read.text
        confirmed = say('yes','confirm-revision',prompt,'writing.confirm_original')
        result = client.post(f"/v1/agent-turns/{confirmed['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        follow = result.json()['turn']['local_evaluation']
        assert follow['status'] == 'local_feedback_required', follow
        local = client.post(f"/v1/agent-turns/{follow['local_feedback_turn_id']}/execute")
        assert local.status_code == 200, local.text
        with db.connect() as c:
            attempt = c.execute('''SELECT a.*,e.hint_level FROM diagnostic_prompt p JOIN diagnostic_attempt a USING(attempt_id)
                JOIN learning_evidence e ON e.evidence_id=a.evidence_id
                WHERE p.prompt_id=?''',(prompt['prompt_id'],)).fetchone()
            assert attempt['submitted_answer'] == manuscript and attempt['hint_level'] >= 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 2
            feedback_plan = c.execute('SELECT objective_id,context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
                (follow['feedback_turn_id'],)).fetchone()
            binding = json.loads(feedback_plan['context_manifest_json'])['original_learning_binding']
            assert binding['prompt_id'] == prompt['prompt_id']
            assert binding['required_hint_level'] == 1
            assert c.execute("SELECT COUNT(*) FROM agent_objective WHERE status='completed'").fetchone()[0] == 0


@pytest.mark.parametrize('kind,quote,cap', [
    ('revision', '我想按刚才的反馈自己改一遍', 'writing.revise_original'),
    ('independent_check', '再来一道不一样的，我自己写', 'writing.independent_check'),
])
def test_actual_asr_http_followup_dispatch_restarts_with_same_prompt(feedback, monkeypatch, kind, quote, cap):
    writing, _, gateway, assessment, _ = feedback
    db, sid, _, parent, _ = writing
    settings = Settings(data_dir=db.path.parent,database_path=db.path,maintenance_enabled=False)
    gateway.output = {'role':'control','control':cap,'quote':quote,'confidence':.99}
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        voice = capture(client,settings,monkeypatch,sid,quote,parent,'followup-voice')
        payload = {'transcript':quote,'client_action_id':'real-followup','voice_input_id':voice,'cloud_text_allowed':True}
        response = client.post(f'/v1/sessions/{sid}/agent-turn',json=payload)
        assert response.status_code == 200, response.text
        turn = response.json()
        assert turn.get('runtime_capability') == cap, turn
        executed = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
        new = executed.json()['execution']['result']['prompt']
        assert new['prompt_id'] != parent['prompt_id']
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        replay = client.post(f'/v1/sessions/{sid}/agent-turn',json=payload)
        assert replay.status_code == 200 and replay.json()['agent_turn_id'] == turn['agent_turn_id'], replay.text
        repeated = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()['execution']['result']['prompt']['prompt_id'] == new['prompt_id']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM writing_followup').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
            binding = json.loads(c.execute('SELECT plan_json FROM agent_plan WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()[0])['local_learning_binding']
            assert binding['prompt_id'] == new['prompt_id'] and binding['assessment_id'] == assessment['assessment_id']
    assert len(gateway.requests) == 1
