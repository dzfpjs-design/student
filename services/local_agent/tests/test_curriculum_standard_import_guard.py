"""Synthetic official-source replacement must not race a frozen disclosure."""
from concurrent.futures import ThreadPoolExecutor
import json
import time

import pytest

from home_study_agent.daily_report_combined_snapshot import CANDIDATE_SCHEMA, freeze, inspect
from home_study_agent.daily_report_observation import prepare
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.pilot_test_telemetry import JOINT_TEST_MEMBER_SCHEMA, PilotTestTelemetryService
from home_study_agent.report_delivery import ReportDeliveryService
from home_study_agent.reports import build_daily_report
from home_study_agent.repository import StudyRepository
from test_curriculum_corpus import create_official_math_standard
from test_curriculum_first_source_http import CHILD, app_client, auth, first_source_fixture, publish


def _root(tmp_path, name, source_key, content):
    root = create_official_math_standard(tmp_path / name, content)
    manifest_path = root / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['sources'][0]['source_key'] = source_key
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    pages = root / manifest['sources'][0]['ocr_pages_filename']
    pages.write_text(json.dumps({'page': 12, 'text': content}, ensure_ascii=False) + '\n', encoding='utf-8')
    return root


def test_reimport_waits_for_disclosure_and_invalidates_the_actual_frozen_snapshot(tmp_path):
    db, _, spec, _ = first_source_fixture(tmp_path)
    root = _root(tmp_path, 'replacement-standard', 'fixture', '合成替换课标内容，不再包含原引用。')
    with app_client(tmp_path, db) as client:
        auth(client)
        result = publish(client, spec)
        item = result['diagnostic_item_id']
        sid = client.post('/v1/sessions', json={'child_id': CHILD, 'client_action_id': 'standard-session'}).json()['session_id']
        path = f'/v1/sessions/{sid}/diagnostic-prompt'
        prompt = client.post(path+'/prepare', json={'track': 'school', 'preferred_item_id': item, 'client_action_id': 'standard-prompt'}).json()['prompt']
        answer = client.post(path+'/answer', json={'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version'],
                             'answer': '2,3', 'client_action_id': 'standard-answer'})
        assert answer.status_code == 200 and answer.json()['attempt']['score_status'] == 'verified_local'
        repo = StudyRepository(db)
        with db.connect() as c:
            c.execute('UPDATE family_rule SET max_learning_minutes=30,task_segment_minutes=20,latest_end_time=NULL WHERE child_id=?', (CHILD,))
            c.commit()
        repo.transition(sid, 'check_in', 'classroom_recall', '开始学习', 'standard-checkin')
        repo.add_recall(sid, '数学', '复习合成题。', 'standard-recall')
        repo.transition(sid, 'classroom_recall', 'homework_capture', '记录完成', 'standard-homework-stage')
        repo.add_homework(sid, '数学', '合成练习', 20, 'written', None, None, None, None, None, 'standard-homework')
        workspace = repo.generate_plan(sid, 'standard-plan')
        repo.transition(sid, 'plan_review', 'study', '确认计划', 'standard-study')
        task = workspace['plan_items'][0]['plan_item_id']
        repo.start_plan_item(task, 'standard-start'); repo.complete_plan_item(task, 20, 'standard-complete')
        repo.transition(sid, 'study', 'reflection', '学习完成', 'standard-reflection-stage')
        repo.complete_with_reflection(sid, '完成合成练习。', '', '可以', '明天复习。', False, 'standard-reflection')
        summary, _ = build_daily_report(repo.report_evidence(sid))
        report = repo.get_daily_report(repo.save_daily_report(sid, summary,
            {'action': {'what': '复习合成题', 'duration_minutes': 10}, 'script': ['先看第一步。'], 'avoid': []})['report_id'])
        reports = ParentReportService(db)
        reports.approve(reports.create_from_daily(report)['snapshot_id'])
        key, address = b's' * 32, 'synthetic-parent@example.test'
        delivery = ReportDeliveryService(db, key); delivery.initialize()
        recipient = delivery.add_recipient(CHILD, 'email', address, '合成家长', True)['recipient_id']
        with db.connect() as c:
            source = prepare(c, CHILD, report['report_date'], recipient)
        visual = delivery.freeze_daily_observation(CHILD, report['report_date'], recipient, source)['snapshot_id']
        telemetry = PilotTestTelemetryService(db, tmp_path); telemetry.initialize(); telemetry.set_diagnostic_consent(address, consented=True)
        with db.connect() as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='combined_diagnostic_member'").fetchone(): c.execute(JOINT_TEST_MEMBER_SCHEMA)
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='combined_daily_snapshot'").fetchone(): c.execute(CANDIDATE_SCHEMA)
            c.commit()
        batch = telemetry.create_combined_batch(report['report_date'], address, [CHILD], key)['batch_id']
        frozen = freeze(telemetry, batch, CHILD, report['report_date'], recipient, visual, key)['snapshot_id']
        assert inspect(telemetry, frozen, key)['current']
        route = '/v1/parent/curriculum-standards/import'; body = {'root_path': str(root)}
        with ThreadPoolExecutor(max_workers=1) as pool:
            with db.external_effect_guard():
                busy = client.post(route, json=body)
                assert busy.status_code == 503 and busy.json()['detail']['code'] == 'CURRICULUM_BUSY'
                assert inspect(telemetry, frozen, key)['current']
                pending = pool.submit(client.post, route, json=body)
                time.sleep(.15); assert not pending.done()
                with db.connect() as c:
                    assert c.execute("SELECT content_text FROM curriculum_standard_page WHERE source_id='std' AND page_number=12").fetchone()[0] == '理解一元二次方程求根公式并求出方程的根。'
            response = pending.result(timeout=5)
        assert response.status_code == 200, response.text
        with pytest.raises(ValueError, match='COMBINED_FORMAL_CHANGED'):
            inspect(telemetry, frozen, key)


def test_new_and_unreferenced_reimports_do_not_wait_for_disclosure(tmp_path):
    db, _, _, _ = first_source_fixture(tmp_path)
    root = _root(tmp_path, 'new-standard', 'synthetic-unreferenced', '独立合成的新课标页面。')
    route = '/v1/parent/curriculum-standards/import'; body = {'root_path': str(root)}
    with app_client(tmp_path, db) as client:
        auth(client)
        with ThreadPoolExecutor(max_workers=1) as pool, db.external_effect_guard():
            for _ in range(2):
                result = pool.submit(client.post, route, json=body).result(timeout=4)
                assert result.status_code == 200, result.text
            result = pool.submit(client.get, '/v1/parent/curriculum-standards').result(timeout=4)
            assert result.status_code == 200, result.text
