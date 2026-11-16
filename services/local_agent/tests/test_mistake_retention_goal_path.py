"""Mistake retention, not mere practice time, changes the next goal path."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from home_study_agent.mistake_book import MistakeBookService
from test_goal_roadmap import flow, score, read, CHILD, ROOT, TARGET
from test_mistake_gate_reliability import prepare, submit


def qualified_target(db):
    from home_study_agent.specialized_supply import SpecializedDiagnosticSupplyService
    supply = SpecializedDiagnosticSupplyService(db)
    supply.enqueue_local('diag-oly-arithmetic-1', CHILD, 'retention-goal-supply')
    supply.process_local_pending()
    candidate = next(r for r in supply.list(CHILD)['items'] if r['seed_item_id'] == 'diag-oly-arithmetic-1')
    return supply.qualify_local(candidate['candidate_id'], CHILD, 'retention-goal-qualify')['diagnostic_item_id']


def stage(db):
    from home_study_agent.specialized_program import SpecializedProgramService
    projection = SpecializedProgramService(db).stage_progress(CHILD, 'competition_math')
    return next(n for d in projection['domains'] for n in d['nodes'] if n['knowledge_node_id'] == TARGET)


def retain_root(flow, monkeypatch):
    db = flow[0]
    score(flow, monkeypatch, days_ago=5)
    with db.connect() as c:
        sid = c.execute("SELECT session_id FROM daily_session WHERE child_id=? AND status='active'", (CHILD,)).fetchone()[0]
    clock = [datetime.now(timezone.utc) - timedelta(days=4)]
    with monkeypatch.context() as patch:
        patch.setattr('home_study_agent.mistake_book.utc_now', lambda: clock[0].isoformat())
        book, mid = prepare((db, None, sid, clock))
        submit(book, mid)
        clock[0] += timedelta(hours=49)
        submit(book, mid, 'delayed_48h')
    return sid, book, mid


def test_planned_specialized_execution_obeys_same_cross_node_prerequisites(flow, monkeypatch):
    from home_study_agent.goal_roadmap import stage_goal_prerequisites
    db = flow[0]
    target = qualified_target(db)
    # A standalone baseline check is allowed; automatic goal progression is not.
    with db.connect() as c:
        before = stage_goal_prerequisites(c, CHILD, TARGET)
    assert before['status'] == 'blocked'
    assert 'knowledge_prerequisite_unverified' in before['blockers']
    _, book, mid = retain_root(flow, monkeypatch)
    ready = stage(db)
    assert ready['next_action']['execution_status'] == 'ready', ready
    assert ready['next_action']['diagnostic_item_id'] == target
    with db.connect() as c:
        basis = stage_goal_prerequisites(c, CHILD, TARGET)
    assert basis['status'] == 'ready'
    book.confirm(mid, CHILD, 'exclude', 'withdraw-before-specialized-execution')
    with db.connect() as c:
        after = stage_goal_prerequisites(c, CHILD, TARGET)
    assert after['status'] == 'blocked'
    assert after['source_version'] != basis['source_version']


@pytest.mark.parametrize('withdrawal', [None, 'before_start', 'before_answer', 'graph_revision', 'wrong_answer'])
def test_retention_to_real_plan_question_answer_and_revised_route(flow, monkeypatch, withdrawal):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from home_study_agent.database import Database
    from home_study_agent.repository import StudyRepository
    from home_study_agent.learning_state import LearningStateService
    from test_learning_runtime_http import NoCloud
    from test_voice_runtime_binding import capture, interpret
    db = flow[0]
    # Keep the synthetic graph stable across app seeding, which inserts missing
    # built-in edges. Inactive records are retained rather than recreated.
    LearningStateService(db).seed_catalog()
    with db.connect() as c:
        c.execute("UPDATE knowledge_edge SET status='inactive' WHERE target_node_id=? AND edge_id!='roadmap-prerequisite'", (TARGET,))
        c.commit()
    exact = qualified_target(db)
    sid, book, mid = retain_root(flow, monkeypatch)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',recall_declared_empty=1,homework_declared_empty=1 WHERE session_id=?", (sid,))
        c.execute('UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=120,competition_minutes=15,pet_min_minutes=0 WHERE child_id=?', (CHILD,))
        rubric = json.loads(c.execute('SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?', (exact,)).fetchone()[0])
        c.commit()
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        response = client.post(f'/v1/sessions/{sid}/plan/generate', json={'client_action_id': 'retention-target-plan'})
        assert response.status_code == 200, response.text
        workspace = response.json()
        items = [p for p in workspace['plan_items'] if p['source_type'] == 'competition_math']
        assert len(items) == 1, [(r.get('obligation_type'), r.get('reason'), r.get('metadata'))
                               for r in workspace['constraint_decisions'] if r.get('obligation_type') == 'competition_math']
        pid = items[0]['plan_item_id']
        for p in workspace['plan_items']:
            if p['plan_item_id'] != pid:
                deferred = client.post(f"/v1/plan-items/{p['plan_item_id']}/reschedule", json={
                    'reason': '合成检查：先执行已核对的目标任务', 'client_action_id': 'defer-' + p['plan_item_id']})
                assert deferred.status_code == 200, deferred.text
        response = client.post(f'/v1/sessions/{sid}/transition', json={'expected_state': 'plan_review',
            'target_state': 'study', 'reason': '确认计划', 'client_action_id': 'retention-study'})
        assert response.status_code == 200, response.text
    # The task and graph basis must survive a complete application reload.
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        if withdrawal == 'before_start':
            book.confirm(mid, CHILD, 'exclude', 'before-goal-start')
        elif withdrawal == 'graph_revision':
            with db.connect() as c:
                c.execute("UPDATE knowledge_edge SET knowledge_version='v2' WHERE edge_id='roadmap-prerequisite'")
                c.commit()
        voice = capture(client, settings, monkeypatch, sid, '开始任务', key='target-start-voice')
        turn = interpret(client, sid, '开始任务', 'retention-start-target', voice)
        started = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert started.status_code == 200, started.text
        result = started.json()['execution']['result']
        if withdrawal in {'before_start', 'graph_revision'}:
            assert result['status'] == 'blocked', result
            assert started.json()['execution']['response_text'].strip()
            with db.connect() as c:
                assert c.execute('SELECT started_at FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0] is None
                assert c.execute('SELECT count(*) FROM diagnostic_prompt WHERE diagnostic_item_id=?', (exact,)).fetchone()[0] == 0
            return
        assert result['status'] == 'active', result
        prompt = result['diagnostic_prompt']['prompt']
        assert prompt['diagnostic_item_id'] == exact
        assert prompt['item']['knowledge_node_id'] == TARGET
        text = '答案是' + ('-99999' if withdrawal == 'wrong_answer' else str(rubric['exact_answers'][0]))
        voice = capture(client, settings, monkeypatch, sid, text, prompt, key='target-answer-voice')
        admitted = interpret(client, sid, text, 'retention-target-answer-turn', voice)
        assert admitted['action']['type'] == 'submit_diagnostic_answer', admitted
        assert admitted['action']['diagnostic_prompt_id'] == prompt['prompt_id']
        if withdrawal == 'before_answer':
            book.confirm(mid, CHILD, 'exclude', 'before-goal-answer')
        answered = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json={
            'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version'],
            'answer': admitted['action']['answer'], 'client_action_id': 'retention-target-answer',
            'authorized_turn_id': admitted['agent_turn_id']})
        assert answered.status_code == 200, answered.text
        if withdrawal == 'before_answer':
            assert answered.json()['status'] == 'invalidated', answered.text
            with db.connect() as c:
                assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE diagnostic_item_id=?', (exact,)).fetchone()[0] == 0
            return
        assert answered.json()['attempt']['correctness'] == (0 if withdrawal == 'wrong_answer' else 1), answered.text
        voice = capture(client, settings, monkeypatch, sid, '这项做完了', key='target-finish-voice')
        finished = interpret(client, sid, '这项做完了', 'retention-target-finish', voice)
        completed = client.post(f"/v1/agent-turns/{finished['agent_turn_id']}/execute")
        assert completed.status_code == 200, completed.text
        assert completed.json()['execution']['result']['status'] == 'completed', completed.text
    with Database(db.path).connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0] == 'completed'
        assert c.execute("SELECT count(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='completed'", (pid,)).fetchone()[0] == 1
    route = read(flow)
    expected = 'recheck_needed' if withdrawal == 'wrong_answer' else 'independent_observed'
    assert next(n for n in route['prerequisite_path'] if n['knowledge_node_id'] == TARGET)['baseline_status'] == expected
    assert route['goal_attainment'] == 'not_established'
    book.confirm(mid, CHILD, 'exclude', 'after-target-completed')
    assert read(flow)['next_daily_action']['knowledge_node_id'] == ROOT


@pytest.mark.parametrize('change', ['source_withdrawn', 'cycle', 'new_failure', 'wrong_child', None])
def test_prerequisite_snapshot_uses_current_evidence_scope_and_readonly_graph(flow, monkeypatch, change):
    from home_study_agent.goal_roadmap import stage_goal_prerequisites
    db = flow[0]
    retain_root(flow, monkeypatch)
    with db.connect() as c:
        if change == 'source_withdrawn':
            c.execute("UPDATE knowledge_node SET status='inactive' WHERE knowledge_node_id=?", (ROOT,))
        elif change == 'cycle':
            c.execute("INSERT INTO knowledge_edge VALUES('test-cycle',?,?,'prerequisite_of','Synthetic cycle','synthetic-catalog','v1','active')", (TARGET, ROOT))
        c.commit()
    if change == 'new_failure':
        score(flow, monkeypatch, item='diag-p4-arithmetic-closed-1', answer='wrong', days_ago=0)
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        result = stage_goal_prerequisites(c, 'child-junior-3' if change == 'wrong_child' else CHILD, TARGET)
        assert result['status'] == ('ready' if change is None else 'blocked'), result
        assert result['cloud_transmission_allowed'] is False
        assert result['mastery_claim_allowed'] is False


def test_mistake_retention_unlocks_next_path_and_withdrawal_restores_prerequisite(flow, monkeypatch):
    db, _, _ = flow
    assert read(flow)['next_daily_action']['knowledge_node_id'] == ROOT
    score(flow, monkeypatch, days_ago=5)
    with db.connect() as c:
        sid = c.execute("SELECT session_id FROM daily_session WHERE child_id=? AND status='active'", (CHILD,)).fetchone()[0]
    clock = [datetime.now(timezone.utc) - timedelta(days=4)]
    with monkeypatch.context() as patch:
        patch.setattr('home_study_agent.mistake_book.utc_now', lambda: clock[0].isoformat())
        book, mid = prepare((db, None, sid, clock))
        submit(book, mid)
        before = read(flow)
        assert before['next_daily_action']['knowledge_node_id'] == ROOT
        clock[0] += timedelta(hours=49)
        submit(book, mid, 'delayed_48h')
    after = read(flow)
    assert after['next_daily_action']['knowledge_node_id'] == TARGET
    assert after['source_version'] != before['source_version']
    assert not after['mastery_claim_allowed'] and after['goal_attainment'] == 'not_established'
    MistakeBookService(db).confirm(mid, CHILD, 'exclude', 'withdraw-retention-for-path')
    revised = read(flow)
    assert revised['next_daily_action']['knowledge_node_id'] == ROOT
    assert revised['source_version'] != after['source_version']
