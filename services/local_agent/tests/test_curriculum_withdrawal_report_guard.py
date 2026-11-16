"""Synthetic HTTP withdrawals serialize with an in-flight disclosure owner."""
from concurrent.futures import ThreadPoolExecutor
import time

import pytest

from home_study_agent.parent_reports import ParentReportService
from home_study_agent.daily_report_combined_snapshot import CANDIDATE_SCHEMA, freeze, inspect
from home_study_agent.daily_report_observation import prepare
from home_study_agent.pilot_test_telemetry import JOINT_TEST_MEMBER_SCHEMA, PilotTestTelemetryService
from home_study_agent.report_delivery import ReportDeliveryService
from home_study_agent.reports import build_daily_report
from home_study_agent.repository import StudyRepository
from test_curriculum_first_source_http import (
    BASE as FIRST_BASE, CHILD as FIRST_CHILD, app_client, auth,
    first_source_fixture, publish as publish_first,
)
from test_curriculum_supply import CHILD as REGULAR_CHILD, prepared
from test_curriculum_supply_http import BASE as REGULAR_BASE, approved, post


@pytest.mark.parametrize('kind', ['first', 'regular'])
def test_parent_withdrawal_waits_for_disclosure_then_invalidates_formal_source(tmp_path, kind):
    if kind == 'first':
        db, _, spec, _ = first_source_fixture(tmp_path)
        child, base, answer = FIRST_CHILD, FIRST_BASE, '2,3'
    else:
        db, _, payload = prepared(tmp_path)
        child, base, answer = REGULAR_CHILD, REGULAR_BASE, '5000'
    with app_client(tmp_path, db) as client:
        auth(client)
        if kind == 'first':
            result = publish_first(client, spec)
        else:
            source = {k: v for k, v in payload.items() if k not in {'prompt', 'expected_answer'}}
            cid = approved(client, {'action_id': 'http-generate', 'expected_version': 0,
                                    'expected_scope_version': 1, 'source': source})
            result = post(client, f'/{cid}/publish', {'action_id': 'p', 'expected_version': 3})
        cid, item = result['candidate_id'], result['diagnostic_item_id']
        sid = client.post('/v1/sessions', json={'child_id': child, 'client_action_id': 'guard-session'}).json()['session_id']
        root = f'/v1/sessions/{sid}/diagnostic-prompt'
        prepared_prompt = client.post(root + '/prepare', json={'track': 'school', 'preferred_item_id': item,
                                      'client_action_id': 'guard-prompt'})
        assert prepared_prompt.status_code == 200, prepared_prompt.text
        prompt = prepared_prompt.json()['prompt']
        scored = client.post(root + '/answer', json={'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version'],
                             'answer': answer, 'client_action_id': 'guard-answer'})
        assert scored.status_code == 200 and scored.json()['attempt']['score_status'] == 'verified_local', scored.text

        repo = StudyRepository(db)
        with db.connect() as c:
            c.execute('UPDATE family_rule SET max_learning_minutes=30,task_segment_minutes=20,latest_end_time=NULL WHERE child_id=?', (child,))
            c.commit()
        repo.transition(sid, 'check_in', 'classroom_recall', '开始学习', 'guard-checkin')
        repo.add_recall(sid, '数学', '复习了合成题。', 'guard-recall')
        repo.transition(sid, 'classroom_recall', 'homework_capture', '记录完成', 'guard-homework-stage')
        repo.add_homework(sid, '数学', '合成练习', 20, 'written', None, None, None, None, None, 'guard-homework')
        workspace = repo.generate_plan(sid, 'guard-plan')
        repo.transition(sid, 'plan_review', 'study', '确认计划', 'guard-study')
        plan_item = workspace['plan_items'][0]['plan_item_id']
        repo.start_plan_item(plan_item, 'guard-start')
        repo.complete_plan_item(plan_item, 20, 'guard-complete')
        repo.transition(sid, 'study', 'reflection', '学习完成', 'guard-reflection-stage')
        repo.complete_with_reflection(sid, '完成合成练习。', '', '可以', '明天复习。', False, 'guard-reflection')
        summary, _ = build_daily_report(repo.report_evidence(sid))
        report = repo.get_daily_report(repo.save_daily_report(sid, summary,
            {'action': {'what': '复习合成题', 'duration_minutes': 10}, 'script': ['先看第一步。'], 'avoid': []})['report_id'])
        reports = ParentReportService(db)
        snapshot = reports.approve(reports.create_from_daily(report)['snapshot_id'])['snapshot_id']
        key = b's' * 32
        address = 'synthetic-parent@example.test'
        delivery = ReportDeliveryService(db, key)
        delivery.initialize()
        recipient = delivery.add_recipient(child, 'email', address, '合成家长', True)['recipient_id']
        with db.connect() as c:
            source = prepare(c, child, report['report_date'], recipient)
        visual = delivery.freeze_daily_observation(child, report['report_date'], recipient, source)['snapshot_id']
        telemetry = PilotTestTelemetryService(db, tmp_path)
        telemetry.initialize()
        telemetry.set_diagnostic_consent(address, consented=True)
        with db.connect() as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='combined_diagnostic_member'").fetchone():
                c.execute(JOINT_TEST_MEMBER_SCHEMA)
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='combined_daily_snapshot'").fetchone():
                c.execute(CANDIDATE_SCHEMA)
            c.commit()
        batch = telemetry.create_combined_batch(report['report_date'], address, [child], key)['batch_id']
        combined = freeze(telemetry, batch, child, report['report_date'], recipient, visual, key)['snapshot_id']
        assert inspect(telemetry, combined, key)['current']
        def source_current():
            with db.connect() as c:
                row = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot,)).fetchone()
                return reports.snapshot_source_current(c, row)
        assert source_current()

        command = {'action_id': 'guard-withdraw', 'expected_version': 4, 'note': '撤回合成题'}
        with ThreadPoolExecutor(max_workers=1) as pool:
            with db.external_effect_guard():
                busy = client.post(base + f'/{cid}/withdraw', json=command)
                assert busy.status_code == 503 and busy.json()['detail']['code'] == 'CURRICULUM_BUSY'
                assert source_current()
                assert inspect(telemetry, combined, key)['current']
                pending = pool.submit(client.post, base + f'/{cid}/withdraw', json=command)
                time.sleep(.15)
                assert not pending.done()
                with db.connect() as c:
                    assert c.execute('SELECT status FROM diagnostic_item WHERE diagnostic_item_id=?', (item,)).fetchone()[0] == 'active'
            response = pending.result(timeout=5)
        assert response.status_code == 200, response.text
        assert response.json()['state'] == 'withdrawn'
        assert not source_current()
        with pytest.raises(ValueError, match='COMBINED_FORMAL_CHANGED'):
            inspect(telemetry, combined, key)


@pytest.mark.parametrize('kind', ['first', 'regular'])
def test_new_independent_draft_does_not_wait_for_disclosure(tmp_path, kind):
    if kind == 'first':
        db, _, spec, _ = first_source_fixture(tmp_path)
        base, path = FIRST_BASE, '/first-sources'
        body = {'action_id': 'free-draft', 'expected_version': 0, 'expected_scope_version': 1, 'source': spec}
    else:
        db, _, payload = prepared(tmp_path)
        base, path = REGULAR_BASE, '/generate'
        source = {k: v for k, v in payload.items() if k not in {'prompt', 'expected_answer'}}
        body = {'action_id': 'free-draft', 'expected_version': 0, 'expected_scope_version': 1, 'source': source}
    with app_client(tmp_path, db) as client:
        auth(client)
        with ThreadPoolExecutor(max_workers=1) as pool, db.external_effect_guard():
            pending = pool.submit(client.post, base + path, json=body)
            response = pending.result(timeout=4)
            assert response.status_code == 200, response.text
            assert response.json()['state'] in {'draft', 'blocked'}
