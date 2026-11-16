"""Carryover accounting follows actual repository writes, not a test-only plan."""
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import os
import time
from uuid import uuid4

import pytest

from home_study_agent import repository
from home_study_agent.repository import StudyRepository, PlanItemStateError, PlanInputError
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from fastapi.testclient import TestClient
from test_agent_continuation import Gateway
from test_learning_input_carryover import carry, grant, plan
from test_learning_input_replan import schedule, inputs, flow, CHILD


@pytest.fixture
def host(carry, monkeypatch):
    db, service, _, clock = carry
    family_zone = ZoneInfo('Asia/Shanghai')

    class FamilyDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = clock[0].astimezone(tz or family_zone)
            return value if tz else value.replace(tzinfo=None)

    class FamilyDate(date):
        @classmethod
        def today(cls):
            return clock[0].astimezone(family_zone).date()

    original_tz = os.environ.get('TZ')
    monkeypatch.setenv('TZ', 'Asia/Shanghai')
    time.tzset()
    monkeypatch.setattr(repository, 'datetime', FamilyDateTime)
    monkeypatch.setattr(repository, 'date', FamilyDate)
    monkeypatch.setattr(repository, 'utc_now', lambda: clock[0].isoformat())
    with db.connect() as c:
        c.execute('UPDATE family_rule SET competition_minutes=0 WHERE child_id=?', (CHILD,))
        c.commit()
    try:
        yield carry, StudyRepository(db)
    finally:
        if original_tz is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = original_tz
        time.tzset()


def test_family_cutoff_still_rejects_carryover_plan(host):
    case, repo = host
    db, service, _, clock = case
    service.review_unplaced(**grant(case))
    clock[0] = clock[0].astimezone(ZoneInfo('Asia/Shanghai')).replace(hour=21, minute=30)
    with pytest.raises(PlanInputError, match='最晚结束时间'):
        repo.generate_plan('today', 'cutoff-plan')
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM daily_plan WHERE session_id='today'").fetchone()[0] == 0
    assert service.list_unplaced(CHILD, 'today')['items'][0]['remaining_minutes'] == 30


def test_actual_plan_accounts_accepted_work_and_reopens_after_restart(host):
    carry, repo = host
    db, service, _, _ = carry
    accepted = service.review_unplaced(**grant(carry))
    workspace = repo.generate_plan('today', 'actual-carry-plan')
    items = [item for item in workspace['plan_items'] if item['homework_id'] == accepted['target_homework_id']]
    assert sum(item['expected_minutes'] for item in items if item['status'] == 'pending') == 30
    assert service.list_unplaced(CHILD, 'today')['items'][0]['remaining_minutes'] == 0
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM learning_input_carryover_plan_receipt').fetchone()[0] == 1
    repo = StudyRepository(db)
    repo.transition('today', 'plan_review', 'study', 'child_confirmed_plan', 'confirm-carried-plan')
    pending = next(item for item in items if item['status'] == 'pending')
    assert repo.start_plan_item(pending['plan_item_id'], 'carried-start')['status'] == 'active'
    assert repo.get_workspace('today')['learning_input_unplaced']['items'][0]['remaining_minutes'] == 0


def test_real_reschedule_reclaims_only_its_allocation_once_and_not_legacy_queue(host):
    carry, repo = host
    db, service, _, _ = carry
    accepted = service.review_unplaced(**grant(carry))
    workspace = repo.generate_plan('today', 'reclaim-plan')
    item = next(row for row in workspace['plan_items'] if row['homework_id'] == accepted['target_homework_id'] and row['status'] == 'pending')
    first = repo.reschedule_plan_item(item['plan_item_id'], 'tomorrow', 'reclaim-once')
    assert first['status'] == 'rescheduled'
    assert StudyRepository(db).reschedule_plan_item(item['plan_item_id'], 'tomorrow', 'reclaim-once') == first
    with db.connect() as c:
        events = c.execute("SELECT result_json FROM learning_input_carryover_event WHERE operation='reclaim'").fetchall()
        assert len(events) == 1
        receipt = json.loads(events[0][0])
        assert receipt['remaining_minutes'] == item['expected_minutes']
        assert not receipt['scope_review_required']
        assert not any(row['source_plan_item_id'] == item['plan_item_id'] for row in repo._deferred_task_candidates(c, CHILD, 'later'))
    with pytest.raises(PlanItemStateError, match='不一致'):
        repo.reschedule_plan_item(item['plan_item_id'], 'changed reason', 'reclaim-once')


def test_real_reschedule_reclaim_write_failure_rolls_back_item_and_event(host):
    carry, repo = host
    db, service, _, _ = carry
    accepted = service.review_unplaced(**grant(carry))
    workspace = repo.generate_plan('today', 'reclaim-failure-plan')
    item = next(row for row in workspace['plan_items'] if row['homework_id'] == accepted['target_homework_id'] and row['status'] == 'pending')
    with db.connect() as c:
        c.execute("CREATE TRIGGER fail_reclaim BEFORE INSERT ON learning_input_carryover_event WHEN NEW.operation='reclaim' BEGIN SELECT RAISE(ABORT,'reclaim_failed'); END")
        c.commit()
    with pytest.raises(Exception, match='reclaim_failed'):
        repo.reschedule_plan_item(item['plan_item_id'], 'tomorrow', 'rollback-reclaim')
    with db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (item['plan_item_id'],)).fetchone()[0] == 'pending'
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE client_action_id='rollback-reclaim'").fetchone()[0] == 0
        c.execute('DROP TRIGGER fail_reclaim'); c.commit()
    assert repo.reschedule_plan_item(item['plan_item_id'], 'tomorrow', 'rollback-reclaim')['status'] == 'rescheduled'


def test_paused_work_keeps_actual_time_and_requires_remaining_content_review(host):
    carry, repo = host
    db, service, _, clock = carry
    accepted = service.review_unplaced(**grant(carry))
    workspace = repo.generate_plan('today', 'partial-reclaim-plan')
    item = next(row for row in workspace['plan_items'] if row['homework_id'] == accepted['target_homework_id'] and row['status'] == 'pending')
    repo.transition('today', 'plan_review', 'study', 'child_confirmed_plan', 'partial-confirm')
    repo.start_plan_item(item['plan_item_id'], 'partial-start')
    clock[0] += timedelta(minutes=5)
    repo.pause_plan_item(item['plan_item_id'], 'partial-pause')
    repo.reschedule_plan_item(item['plan_item_id'], 'remaining tomorrow', 'partial-reschedule')
    with db.connect() as c:
        receipt = json.loads(c.execute("SELECT result_json FROM learning_input_carryover_event WHERE operation='reclaim'").fetchone()[0])
        assert receipt['status'] == 'awaiting_scope_review'
        assert receipt['scope_review_required'] and receipt['remaining_minutes'] == 0
        assert receipt['maximum_remaining_minutes'] == item['expected_minutes'] - 5
        saved = c.execute('SELECT active_seconds,status FROM plan_item WHERE plan_item_id=?', (item['plan_item_id'],)).fetchone()
        assert saved['active_seconds'] == 300 and saved['status'] == 'rescheduled'
        assert c.execute('SELECT COUNT(*) FROM learning_evidence').fetchone()[0] == 0


def test_remaining_scope_voice_uses_real_dispatcher_and_restarts_into_allocation(host, tmp_path):
    carry, repo = host
    db, service, _, clock = carry
    accepted = service.review_unplaced(**grant(carry))
    workspace = repo.generate_plan('today', 'scope-voice-plan')
    item = next(row for row in workspace['plan_items'] if row['homework_id'] == accepted['target_homework_id'] and row['status'] == 'pending')
    repo.transition('today', 'plan_review', 'study', 'child_confirmed_plan', 'scope-voice-confirm')
    repo.start_plan_item(item['plan_item_id'], 'scope-voice-start')
    clock[0] += timedelta(minutes=5)
    repo.pause_plan_item(item['plan_item_id'], 'scope-voice-pause')
    repo.reschedule_plan_item(item['plan_item_id'], 'remaining tomorrow', 'scope-voice-reschedule')
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    gateway = Gateway({})
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        listing = client.post('/v1/sessions/today/agent-turn', json={'transcript': '昨天没排完的作业有哪些', 'client_action_id': 'scope-list'})
        assert listing.status_code == 200, listing.text
        turn = client.post('/v1/sessions/today/agent-turn', json={'transcript': '还剩第3到5题预计五分钟', 'client_action_id': 'scope-confirm'})
        assert turn.status_code == 200, turn.text
        assert turn.json()['action']['type'] == 'review_learning_input_reclaimed_scope'
        execution = client.post(f"/v1/agent-turns/{turn.json()['agent_turn_id']}/execute")
        assert execution.status_code == 200, execution.text
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        turn = client.post('/v1/sessions/today/agent-turn', json={'transcript': '今天继续', 'client_action_id': 'scope-continue'})
        assert turn.status_code == 200, turn.text
        assert turn.json()['action']['type'] == 'review_learning_input_unplaced'
        execution = client.post(f"/v1/agent-turns/{turn.json()['agent_turn_id']}/execute")
        assert execution.status_code == 200, execution.text
        with db.connect() as c:
            confirmed = c.execute("SELECT result_json FROM learning_input_carryover_event WHERE operation='confirm_scope'").fetchall()
            assert len(confirmed) == 1 and json.loads(confirmed[0][0])['remaining_minutes'] == 5
            assert c.execute("SELECT COUNT(*) FROM plan_item WHERE title LIKE '%第3到5题%' AND status='pending'").fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM learning_evidence').fetchone()[0] == 0
        assert gateway.requests == []


def test_plan_receipt_failure_rolls_back_actual_plan_and_preserves_unallocated_work(host):
    carry, repo = host
    db, service, _, _ = carry
    service.review_unplaced(**grant(carry))
    with db.connect() as c:
        c.execute("CREATE TRIGGER fail_carry_receipt BEFORE INSERT ON learning_input_carryover_plan_receipt BEGIN SELECT RAISE(ABORT,'receipt_failed'); END")
        c.commit()
    with pytest.raises(Exception, match='receipt_failed'):
        repo.generate_plan('today', 'plan-with-failure')
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM daily_plan WHERE session_id='today'").fetchone()[0] == 0
        c.execute('DROP TRIGGER fail_carry_receipt')
        c.commit()
    assert service.list_unplaced(CHILD, 'today')['items'][0]['remaining_minutes'] == 30
    repo.generate_plan('today', 'plan-with-failure')
    assert service.list_unplaced(CHILD, 'today')['items'][0]['remaining_minutes'] == 0


@pytest.mark.parametrize('erase_binding', [False, True])
def test_unallocated_placeholders_do_not_duplicate_the_next_day_legacy_queue(host, erase_binding):
    carry, repo = host
    db, service, _, _ = carry
    with db.connect() as c:
        c.execute('UPDATE family_rule SET max_learning_minutes=20 WHERE child_id=?', (CHILD,))
        c.commit()
    accepted = service.review_unplaced(**grant(carry))
    workspace = repo.generate_plan('today', 'partial-plan')
    placeholders = [row for row in workspace['plan_items'] if row['homework_id'] == accepted['target_homework_id'] and row['status'] == 'rescheduled']
    assert placeholders and service.list_unplaced(CHILD, 'today')['items'][0]['remaining_minutes'] == 15
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed',ended_at='2026-09-08T11:00:00+00:00' WHERE session_id='today'")
        if erase_binding:
            c.execute('DELETE FROM learning_input_replan_item WHERE plan_item_id=?', (placeholders[0]['plan_item_id'],))
        candidates = repo._deferred_task_candidates(c, CHILD, 'another-day')
        assert not any(row['source_plan_item_id'] in {item['plan_item_id'] for item in placeholders} for row in candidates)


@pytest.mark.parametrize('change', ['source', 'binding'])
def test_actual_resume_revalidates_original_carryover_source(carry, change):
    db, service, _, _ = carry
    plan(db, total=0)
    result = service.review_unplaced(**grant(carry))
    item = next(row for row in result['newitems'] if row['source_type'] == 'school_homework')
    repo = StudyRepository(db)
    repo.start_plan_item(item['plan_item_id'], 'start')
    repo.pause_plan_item(item['plan_item_id'], 'pause')
    with db.connect() as c:
        if change == 'source':
            c.execute("UPDATE homework_item SET title='different original' WHERE homework_id='math-a'")
        else:
            c.execute('DELETE FROM learning_input_replan_item WHERE plan_item_id=?', (item['plan_item_id'],))
        c.commit()
    with pytest.raises(PlanItemStateError):
        StudyRepository(db).resume_plan_item(item['plan_item_id'], 'stale-resume')


@pytest.mark.parametrize('scope', ['learning', 'media', 'all_history'])
def test_export_and_erasure_include_carryover_history(host, tmp_path, scope):
    carry, repo = host
    db, service, _, _ = carry
    service.review_unplaced(**grant(carry))
    repo.generate_plan('today', 'export-plan')
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    with open(privacy.export_child(CHILD)['path'], encoding='utf-8') as stream:
        exported = json.load(stream)
    assert exported['learning_input_carryover_events']
    assert exported['learning_input_carryover_plan_receipts']
    privacy.delete_child_scope('child-junior-3', scope, '确认删除', str(uuid4()))
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM learning_input_carryover_event').fetchone()[0] > 0
    privacy.delete_child_scope(CHILD, scope, '确认删除', str(uuid4()))
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM learning_input_carryover_event').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM learning_input_carryover_plan_receipt').fetchone()[0] == 0
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('lost_receipt', [False, True])
def test_real_voice_dispatch_keeps_original_obligation_and_survives_restart(host, tmp_path, lost_receipt):
    carry, repo = host
    db, service, _, _ = carry
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    gateway = Gateway({})
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        turn = client.post('/v1/sessions/today/agent-turn', json={
            'transcript': '昨天没安排的口算今天继续', 'client_action_id': 'voice-carry'})
        assert turn.status_code == 200, turn.text
        tid = turn.json()['agent_turn_id']
        path = f'/v1/agent-turns/{tid}/execute'
        if lost_receipt:
            with db.connect() as c:
                c.execute("""CREATE TRIGGER lose_carry_result BEFORE UPDATE OF status ON agent_step
                    WHEN NEW.status='completed' AND NEW.capability_name='learning_inputs.review_unplaced'
                    BEGIN SELECT RAISE(ABORT,'lost_executor_receipt'); END""")
                c.commit()
        result = client.post(path)
        assert result.status_code == (500 if lost_receipt else 200), result.text
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM learning_input_carryover_event WHERE operation='continue_today'").fetchone()[0] == 1
            if lost_receipt:
                c.execute('DROP TRIGGER lose_carry_result')
                c.commit()
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        replay = client.post(path)
        assert replay.status_code == 200, replay.text
        workspace = repo.get_workspace('today')
        assert len(workspace['homework']) == 1
        assert workspace['learning_input_unplaced']['items'][0]['remaining_minutes'] == 30
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM learning_input_carryover_event').fetchone()[0] == 1
        assert gateway.requests == []
