"""Replacement plan tasks retain source gates through repository and erasure."""
import json
from uuid import uuid4

import pytest

from home_study_agent.repository import StudyRepository, PlanItemStateError
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from fastapi.testclient import TestClient
from test_agent_continuation import Gateway
from test_learning_input_replan import schedule, revised, inputs, flow, CHILD


def replacement(schedule):
    _, service, _ = schedule
    event = revised(schedule)
    result = service.execute_replan(CHILD, event, 'host-replan')
    return next(item for item in result['newitems'] if item['source_type'] == 'school_homework')


@pytest.mark.parametrize('state', ['current', 'changed', 'deleted_binding'])
def test_repository_start_checks_revised_source_in_actual_write_transaction(schedule, state):
    db, sid, _ = schedule[0]
    item = replacement(schedule)
    with db.connect() as c:
        if state == 'changed':
            c.execute("UPDATE homework_item SET title='不同的作业' WHERE homework_id='math-a'")
        elif state == 'deleted_binding':
            c.execute('DELETE FROM learning_input_replan_item WHERE plan_item_id=?', (item['plan_item_id'],))
        c.commit()
    repo = StudyRepository(db)
    if state == 'current':
        assert repo.start_plan_item(item['plan_item_id'], 'actual-start')['status'] == 'active'
    else:
        with pytest.raises(PlanItemStateError, match='来源或计划已变化'):
            repo.start_plan_item(item['plan_item_id'], 'invalid-start')
        with db.connect() as c:
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (item['plan_item_id'],)).fetchone()[0] == 'pending'
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE client_action_id='invalid-start'").fetchone()[0] == 0


def test_paused_replacement_cannot_resume_after_source_changes(schedule):
    db, _, _ = schedule[0]
    item = replacement(schedule)
    repo = StudyRepository(db)
    repo.start_plan_item(item['plan_item_id'], 'start-before-change')
    repo.pause_plan_item(item['plan_item_id'], 'pause-before-change')
    with db.connect() as c:
        before = tuple(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (item['plan_item_id'],)).fetchone())
        c.execute("UPDATE homework_item SET title='不同的作业' WHERE homework_id='math-a'")
        c.commit()
    with pytest.raises(PlanItemStateError, match='来源或计划已变化'):
        repo.resume_plan_item(item['plan_item_id'], 'resume-stale')
    with db.connect() as c:
        assert tuple(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (item['plan_item_id'],)).fetchone()) == before


@pytest.mark.parametrize('scope', ['learning', 'media', 'all_history'])
def test_replan_export_erasure_and_other_child_isolation(schedule, tmp_path, scope):
    db, _, _ = schedule[0]
    item = replacement(schedule)
    service = PrivacyLifecycleService(db, tmp_path)
    service.initialize()
    with open(service.export_child(CHILD)['path'], encoding='utf-8') as stream:
        saved = json.load(stream)
    assert len(saved['learning_input_replan_receipts']) == 1
    assert saved['learning_input_replan_items']
    service.delete_child_scope('child-junior-3', scope, '确认删除', str(uuid4()))
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM learning_input_replan_receipt').fetchone()[0] == 1
    service.delete_child_scope(CHILD, scope, '确认删除', str(uuid4()))
    with db.connect() as c:
        for table in ('learning_input_replan_receipt', 'learning_input_replan_command', 'learning_input_replan_item', 'learning_input_replan_unplaced'):
            assert c.execute(f'SELECT COUNT(*) FROM {table} WHERE child_id=?', (CHILD,)).fetchone()[0] == 0
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        if scope != 'all_history':
            marker = c.execute("SELECT payload_json FROM plan_item_event WHERE plan_item_id=? AND event_type='revision_replanned'", (item['plan_item_id'],)).fetchone()
            assert json.loads(marker[0]) == {'source_deleted': True}
    if scope != 'all_history':
        with pytest.raises(PlanItemStateError):
            StudyRepository(db).start_plan_item(item['plan_item_id'], 'deleted-source-cannot-start')


@pytest.mark.parametrize('lost_storage', [False, True])
def test_voice_confirm_replans_actual_work_and_recovers_without_duplicate_revision(schedule, tmp_path, lost_storage):
    db, sid, _ = schedule[0]
    words = '口算改成30分钟'
    gateway = Gateway({'objective': {'mode': 'none'}, 'utterance_binding': {'role': 'correction', 'quote': words}, 'next_step': {
        'capability': 'learning_inputs.propose_revision', 'arguments': {'kind': 'homework', 'operation': 'update',
            'target_quote': '口算', 'request_quote': words, 'changes': {'expected_minutes': 30}, 'change_quotes': {'expected_minutes': '30分钟'}}},
        'spoken_transition': '我先核对这项作业。', 'requires_child_reply': False, 'confidence': .99})
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        turn = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': words, 'client_action_id': 'host-revise-original'})
        assert turn.status_code == 200, turn.text
        response = client.post(f"/v1/agent-turns/{turn.json()['agent_turn_id']}/execute")
        assert response.status_code == 200, response.text
        confirmation = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': '确认，就这样改', 'client_action_id': 'host-confirm-original'})
        assert confirmation.status_code == 200, confirmation.text
        turn_id = confirmation.json()['agent_turn_id']
        path = f'/v1/agent-turns/{turn_id}/execute'
        if lost_storage:
            with db.connect() as c:
                c.execute("CREATE TRIGGER fail_host_replan BEFORE INSERT ON learning_input_replan_receipt BEGIN SELECT RAISE(ABORT,'temporary_storage_failure'); END")
                c.commit()
        response = client.post(path)
        if lost_storage:
            assert response.status_code == 500, response.text
            with db.connect() as c:
                assert c.execute("SELECT expected_minutes FROM homework_item WHERE homework_id='math-a'").fetchone()[0] == 30
                assert c.execute('SELECT COUNT(*) FROM learning_input_revision_event').fetchone()[0] == 1
                assert c.execute('SELECT COUNT(*) FROM learning_input_replan_receipt').fetchone()[0] == 0
                c.execute('DROP TRIGGER fail_host_replan')
                c.commit()
        else:
            assert response.status_code == 200, response.text
            assert '补排了30分钟' in response.json()['turn']['response_text']
        calls = len(gateway.requests)
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        response = client.post(path)
        assert response.status_code == 200, response.text
        assert '补排了30分钟' in response.json()['turn']['response_text']
        assert response.json()['execution']['result']['replan']['receipt']['allocated_minutes'] == 30
        assert client.post(path).status_code == 200
        assert len(gateway.requests) == calls
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM learning_input_revision_event').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM learning_input_replan_receipt').fetchone()[0] == 1
            assert c.execute("SELECT SUM(expected_minutes) FROM plan_item WHERE source_type='school_homework' AND status='pending' AND homework_id='math-a'").fetchone()[0] == 30
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='old'").fetchone()[0] == 'cancelled'
