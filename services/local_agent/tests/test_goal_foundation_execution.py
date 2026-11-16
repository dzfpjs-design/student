"""Missing foundations become actual work, not just a blocked challenge."""
import json
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository
from test_goal_roadmap import flow, CHILD, ROOT, TARGET
from test_mistake_retention_goal_path import qualified_target
from test_learning_runtime_http import NoCloud
from test_voice_runtime_binding import capture, interpret


@pytest.fixture
def foundation(flow):
    db = flow[0]
    LearningStateService(db).seed_catalog()
    qualified_target(db)
    sid = StudyRepository(db).start_session(CHILD, 'foundation-session')['session_id']
    with db.connect() as c:
        c.execute("UPDATE knowledge_edge SET status='inactive' WHERE target_node_id=? AND edge_id!='roadmap-prerequisite'", (TARGET,))
        c.execute("UPDATE daily_session SET state='homework_capture',recall_declared_empty=1,homework_declared_empty=1 WHERE session_id=?", (sid,))
        c.execute('UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=120,competition_minutes=15,pet_min_minutes=0 WHERE child_id=?', (CHILD,))
        c.commit()
    return db, sid, Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False)


def planned(client, sid):
    response = client.post(f'/v1/sessions/{sid}/plan/generate', json={'client_action_id': 'foundation-plan'})
    assert response.status_code == 200, response.text
    workspace = response.json()
    items = [p for p in workspace['plan_items'] if p['task_type'] == 'goal_prerequisite']
    assert len(items) == 1, [(r['source_type'], r['title']) for r in workspace['plan_items']]
    assert items[0]['expected_minutes'] <= 15
    assert not any(p['source_type'] == 'competition_math' and p['task_type'] != 'goal_prerequisite' for p in workspace['plan_items'])
    return workspace, items[0]


def add_other_target(db):
    with db.connect() as c:
        for new, original in [('aaa-foundation', ROOT), ('aaa-target', TARGET)]:
            c.execute('''INSERT INTO knowledge_node
                SELECT ?,node_type,track,subject,grade_band,?,title,description,
                source_ref,knowledge_version,status FROM knowledge_node WHERE knowledge_node_id=?''',
                (new, new, original))
        c.execute("INSERT INTO knowledge_edge VALUES('other-foundation-edge','aaa-foundation','aaa-target','prerequisite_of','Synthetic other branch','synthetic-catalog','v1','active')")
        c.commit()


@pytest.mark.parametrize('change', [None, 'wrong_answer', 'source_withdrawn', 'graph_changed', 'task_changed', 'resume', 'other_target',
    'hint', 'hint_source_withdrawn', 'hint_graph_changed', 'hint_task_changed', 'hint_resume'])
def test_foundation_plan_voice_execution_restart_and_feedback(foundation, monkeypatch, change):
    db, sid, settings = foundation
    has_hint = bool(change and change.startswith('hint'))
    mutation = change.removeprefix('hint_') if has_hint else change
    gateway = NoCloud()
    if has_hint:
        from test_tutor_child_model_controls import RecordingGateway
        gateway = RecordingGateway()
        gateway.configured = False
    if change == 'other_target':
        add_other_target(db)
        from home_study_agent.goal_roadmap import GoalRoadmapService
        route = GoalRoadmapService(db).read(CHILD, 'competition_math_path')
        assert route['next_daily_action']['knowledge_node_id'] == 'aaa-foundation'
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        workspace, item = planned(client, sid)
        pid = item['plan_item_id']
        for p in workspace['plan_items']:
            if p['plan_item_id'] != pid:
                response = client.post(f"/v1/plan-items/{p['plan_item_id']}/reschedule", json={
                    'reason': '合成任务检查', 'client_action_id': 'defer-' + p['plan_item_id']})
                assert response.status_code == 200
        response = client.post(f'/v1/sessions/{sid}/transition', json={'expected_state': 'plan_review',
            'target_state': 'study', 'reason': '确认', 'client_action_id': 'foundation-study'})
        assert response.status_code == 200, response.text
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        vid = capture(client, settings, monkeypatch, sid, '开始任务', key='foundation-start-audio')
        turn = interpret(client, sid, '开始任务', 'foundation-start', vid)
        response = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert response.status_code == 200, response.text
        result = response.json()['execution']['result']
        assert result['status'] == 'active', result
        prompt = result['diagnostic_prompt']['prompt']
        assert prompt['item']['knowledge_node_id'] == ROOT
        if has_hint:
            text = '这道题给我一个提示'
            vid = capture(client, settings, monkeypatch, sid, text, prompt, key='foundation-hint-audio')
            hint_turn = interpret(client, sid, text, 'foundation-hint', vid)
            assert hint_turn['action'].get('diagnostic_prompt_id') == prompt['prompt_id'], hint_turn['action']
            action = hint_turn['action']
            # Match the renderer's model bridge, then acknowledge its real receipt.
            # This fixture supplies Tutor output, not semantic planner responses.
            gateway.configured = True
            taught = client.post(f'/v1/sessions/{sid}/tutor', json={
                'subject': action['subject'], 'question': action['question'], 'hint_level': action['hint_level'],
                'prompt_id': action['diagnostic_prompt_id'], 'item_version': action['diagnostic_item_version'],
                'client_action_id': 'foundation-tutor'})
            assert taught.status_code == 200, taught.text
            taught = taught.json()
            assert taught['question'] == prompt['item']['prompt']
            assert len(gateway.requests) == 1
            from datetime import datetime, timezone
            receipt = {'capability': 'learning.explain', 'execution_location': 'local_model',
                'action_type': 'ask_tutor', 'status': 'completed', 'completed_at': datetime.now(timezone.utc).isoformat(),
                'output': {'tutor_turn_id': taught['turn_id'], 'provider': taught['provider'], 'model': taught['model'],
                    'response_kind': taught['response_kind'], 'verification_status': taught['verification_status'],
                    'safety_status': taught['safety_status'], 'blackboard_present': bool(taught['output_envelope']['result'].get('blackboard'))}}
            acknowledged = client.put(f"/v1/agent-turns/{hint_turn['agent_turn_id']}/outcome", json={
                'final_response': taught['answer'], 'tool_status': 'completed', 'result': {'execution_receipt': receipt}})
            assert acknowledged.status_code == 200, acknowledged.text
            gateway.configured = False
        if mutation == 'resume':
            StudyRepository(db).pause_plan_item(pid, 'foundation-pause', 'child_requested_pause')
            vid = capture(client, settings, monkeypatch, sid, '继续任务', key='foundation-resume-audio')
            resumed_turn = interpret(client, sid, '继续任务', 'foundation-resume', vid)
            resumed = client.post(f"/v1/agent-turns/{resumed_turn['agent_turn_id']}/execute")
            assert resumed.status_code == 200, resumed.text
            assert resumed.json()['execution']['result']['status'] == 'active', resumed.text
            with db.connect() as c:
                assert c.execute('SELECT count(*) FROM diagnostic_prompt WHERE origin_session_id=?', (sid,)).fetchone()[0] == 1
                assert c.execute('SELECT prompt_id FROM diagnostic_prompt WHERE origin_session_id=?', (sid,)).fetchone()[0] == prompt['prompt_id']
        with db.connect() as c:
            rubric = json.loads(c.execute('SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?',
                (prompt['diagnostic_item_id'],)).fetchone()[0])
        text = '答案是' + ('-999' if change == 'wrong_answer' else str(rubric['exact_answers'][0]))
        vid = capture(client, settings, monkeypatch, sid, text, prompt, key='foundation-answer-audio')
        answer = interpret(client, sid, text, 'foundation-answer', vid)
        assert answer['action']['type'] == 'submit_diagnostic_answer', answer
        with db.connect() as c:
            if mutation == 'source_withdrawn':
                c.execute("UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?", (prompt['diagnostic_item_id'],))
            elif mutation == 'graph_changed':
                c.execute("UPDATE knowledge_edge SET knowledge_version='v2' WHERE edge_id='roadmap-prerequisite'")
            elif mutation == 'task_changed':
                c.execute("UPDATE plan_item SET task_type='reasoning' WHERE plan_item_id=?", (pid,))
            c.commit()
        response = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json={
            'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version'], 'answer': answer['action']['answer'],
            'authorized_turn_id': answer['agent_turn_id'], 'client_action_id': 'foundation-score'})
        assert response.status_code == 200, response.text
        if mutation in {'source_withdrawn', 'graph_changed', 'task_changed'}:
            assert response.json()['status'] == 'invalidated', response.text
            with db.connect() as c:
                assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0] == 0
            return
        assert response.json()['attempt'] is not None, response.json()
        assert response.json()['attempt']['correctness'] == (0 if change == 'wrong_answer' else 1)
        vid = capture(client, settings, monkeypatch, sid, '这项做完了', key='foundation-end-audio')
        turn = interpret(client, sid, '这项做完了', 'foundation-end', vid)
        response = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert response.status_code == 200, response.text
        assert response.json()['execution']['result']['status'] == 'completed', response.text
    with Database(db.path).connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0] == 'completed'
        assert c.execute('SELECT count(*) FROM review_task WHERE child_id=? AND knowledge_node_id=?', (CHILD, ROOT)).fetchone()[0] > 0
    from home_study_agent.goal_roadmap import GoalRoadmapService
    route = GoalRoadmapService(db).read(CHILD, 'competition_math_path')
    root = next(n for n in route['prerequisite_path'] if n['knowledge_node_id'] == ROOT)
    expected_status = 'recheck_needed' if change == 'wrong_answer' else 'supported_only' if has_hint else 'independent_observed'
    assert root['baseline_status'] == expected_status
    assert route['next_daily_action']['knowledge_node_id'] == ('aaa-foundation' if change == 'other_target' else ROOT)


@pytest.mark.parametrize('constraint', ['budget', 'segment', 'source'])
@pytest.mark.parametrize('other_branch', [False, True])
def test_missing_capacity_or_source_stays_explicit_without_fake_task(foundation, constraint, other_branch):
    db, sid, settings = foundation
    if other_branch:
        add_other_target(db)
    with db.connect() as c:
        if constraint == 'budget':
            c.execute('UPDATE family_rule SET competition_minutes=5 WHERE child_id=?', (CHILD,))
        elif constraint == 'segment':
            c.execute('UPDATE family_rule SET task_segment_minutes=5 WHERE child_id=?', (CHILD,))
        else:
            c.execute("UPDATE diagnostic_item SET status='withdrawn' WHERE knowledge_node_id=?", (ROOT,))
        c.commit()
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        response = client.post(f'/v1/sessions/{sid}/plan/generate', json={'client_action_id': 'foundation-constrained'})
        assert response.status_code == 200, response.text
        data = response.json()
        assert not any(p['source_type'] == 'competition_math' for p in data['plan_items'])
        missing = [r for r in data['constraint_decisions'] if r['obligation_type'] == 'competition_math']
        assert missing and all(r['allocated_minutes'] == 0 for r in missing), missing
        assert any(ROOT == r.get('metadata', {}).get('planning_evidence', {}).get('knowledge_node_id') for r in missing)
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0] == 0


def test_existing_due_review_carries_goal_support_without_duplicate_work(foundation):
    db, sid, settings = foundation
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    LearningStateService(db).submit_diagnostic(sid, 'diag-p4-arithmetic-closed-1', 'wrong', 'foundation-wrong')
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture' WHERE session_id=?", (sid,))
        c.commit()
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        response = client.post(f'/v1/sessions/{sid}/plan/generate', json={'client_action_id': 'foundation-reuse'})
        assert response.status_code == 200, response.text
        data = response.json()
        assert not any(p['source_type'] == 'competition_math' for p in data['plan_items'])
        with db.connect() as c:
            decisions = [json.loads(r[0]) for r in c.execute('SELECT metadata_json FROM plan_constraint_decision WHERE plan_id IN (SELECT plan_id FROM daily_plan WHERE session_id=?)',
                (sid,))]
        supports = [r['planning_evidence']['goal_foundation_support'] for r in decisions
                    if r.get('planning_evidence', {}).get('goal_foundation_support')]
        assert len(supports) == 1
        assert supports[0]['knowledge_node_id'] == ROOT and supports[0]['target_node_id'] == TARGET
        assert not supports[0]['mastery_claim_allowed']


@pytest.mark.parametrize('focus', [TARGET, ROOT, 'missing-target'])
def test_action_focus_preserves_complete_goal_scope(foundation, focus):
    from datetime import datetime, timezone
    from home_study_agent.family_clock import family_today
    from home_study_agent.goal_roadmap import build_goal_roadmap
    db = foundation[0]
    add_other_target(db)
    now = datetime.now(timezone.utc)
    with db.connect() as c:
        program = dict(c.execute("SELECT * FROM goal_program WHERE child_id=? AND track='competition_math_path'", (CHILD,)).fetchone())
        full = build_goal_roadmap(c, program, today=family_today(now), feedback_now=now)
        focused = build_goal_roadmap(c, program, today=family_today(now), feedback_now=now, action_target_node_id=focus)
        for key in ('baseline', 'prerequisite_path', 'cycles', 'milestones', 'burden', 'goal_attainment'):
            assert focused[key] == full[key], key
        assert full['next_daily_action']['knowledge_node_id'] == 'aaa-foundation'
        assert focused['action_target_node_id'] == focus
        if focus == TARGET:
            assert focused['next_daily_action']['knowledge_node_id'] == ROOT
        else:
            assert focused['next_daily_action'] is None
            assert any(r['code'] == 'action_target_unavailable' for r in focused['risks'])
        c.execute("INSERT INTO knowledge_edge VALUES('other-cycle','aaa-target','aaa-foundation','prerequisite_of','Synthetic cycle','synthetic-catalog','v1','active')")
        unsafe = build_goal_roadmap(c, program, today=family_today(now), feedback_now=now, action_target_node_id=focus)
        assert unsafe['next_daily_action'] is None
        assert any(r['code'] == 'prerequisite_cycle' for r in unsafe['risks'])
        assert not unsafe['mastery_claim_allowed'] and not unsafe['cloud_transmission_allowed']
