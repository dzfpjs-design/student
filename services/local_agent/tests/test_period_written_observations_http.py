"""Actual report TCP/persistence, with an explicitly synthetic score authority."""
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest

from home_study_agent.period_reports import PeriodReportService
from home_study_agent.repository import StudyRepository
from home_study_agent.reports import build_daily_report
import test_school_open_reviews as school
from test_school_review_start_integrity_http import _http_client
from test_writing_calibrated_gate import authority


@pytest.fixture
def report_case(tmp_path, monkeypatch):
    monkeypatch.setattr(school, 'TestClient', _http_client)
    yield from school.school_case.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param='public'))


def finish_daily(case):
    db, _, _, sid = case[:4]
    repository = StudyRepository(db)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
        c.commit()
    repository.complete_with_reflection(sid, '', '', '', '', True, 'written-report-complete')
    summary, coaching = build_daily_report(repository.report_evidence(sid))
    return repository.save_daily_report(sid, summary, coaching)


def generate(client, headers, kind):
    response = client.post(f'/v1/children/{school.CHILD}/period-reports/generate?report_type={kind}', headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('score', [0, 3])
def test_qualified_original_survives_periods_restart_and_report_withdrawal(report_case, monkeypatch, score):
    db, client, headers = report_case[:3]
    evidence, _, _, _ = authority(monkeypatch, report_case, score)
    daily = finish_daily(report_case)
    if score == 3:
        assert evidence in [ref for stage in daily['summary']['specialized_stage_reports'] for ref in stage['evidence_refs']]
    reports = [generate(client, headers, kind) for kind in ('weekly', 'monthly')]
    with db.connect() as c:
        ref = c.execute('SELECT evidence_ref FROM period_report_evidence_reference WHERE evidence_id=?', (evidence,)).fetchone()[0]
    for report in reports:
        summary = report['content']['learning_summary']
        observations = summary.get('qualified_observations', [])
        assert len(observations) == 1, summary
        assert observations[0]['evidence_ref'] == ref
        assert observations[0]['score_status'] == 'calibrated_model'
        assert observations[0]['passed'] is (score == 3)
        assert observations[0]['mastery_claim_allowed'] is False
        assert summary['coverage']['qualified_observation_count'] == 1
        assert summary['coverage']['answered_evidence_count'] == 0
        assert summary['coverage']['sufficient_for_mastery_claim'] is False
        assert ref in report['content']['parent_coaching']['evidence_refs']
        assert report['content']['parent_coaching']['focus'] == 'period_review'

    client = client.school_reopen()
    headers = {'X-HSC-Parent': client.post('/v1/parent/unlock', json={'pin': '8264'}).json()['parent_token']}
    service = PeriodReportService(db)
    for old in reports:
        assert service.get(old['period_report_id'])['content']['learning_summary']['qualified_observations'] == old['content']['learning_summary']['qualified_observations']
    assert service.retract_evidence(school.CHILD, ref, '核对这一条原稿', 'report-retract')
    for kind in ('weekly', 'monthly'):
        assert generate(client, headers, kind)['content']['learning_summary']['qualified_observations'] == []
    assert service.restore_evidence(school.CHILD, ref, '原稿记录已核对', 'report-restore')
    assert generate(client, headers, 'weekly')['content']['learning_summary']['qualified_observations'][0]['evidence_ref'] == ref
    with db.connect() as c:
        assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE evidence_id=?', (evidence,)).fetchone()[0] is None
        assert c.execute('SELECT count(*) FROM rubric_review_event').fetchone()[0] == 0


def test_human_retraction_removes_period_observation_without_falling_back(report_case, monkeypatch):
    db, client, headers = report_case[:3]
    _, _, _, (path, body) = authority(monkeypatch, report_case, 3)
    finish_daily(report_case)
    assert generate(client, headers, 'weekly')['content']['learning_summary'].get('qualified_observations')
    assert client.post(path, headers=headers, json=body).status_code == 200
    human = generate(client, headers, 'weekly')['content']['learning_summary']['qualified_observations']
    assert len(human) == 1 and human[0]['score_status'] == 'reviewed_human'
    revoked = client.post(path, headers=headers, json=dict(operation='retract', expected_revision=1,
        client_action_id='report-human-retract', reason='原评分需要重新核实'))
    assert revoked.status_code == 200, revoked.text
    assert generate(client, headers, 'weekly')['content']['learning_summary']['qualified_observations'] == []


def test_period_observations_recheck_child_window_source_and_certificate(report_case, monkeypatch):
    from home_study_agent.learning_rubric import RubricEvidenceService
    db, client, headers, _, baseline = report_case[:5]
    _, _, _, _ = authority(monkeypatch, report_case, 3)
    finish_daily(report_case)
    before = generate(client, headers, 'weekly')
    service = PeriodReportService(db)
    assert service.generate('child-primary-4', 'weekly')['content']['learning_summary']['qualified_observations'] == []
    earlier = datetime.now(timezone.utc).date() - timedelta(days=60)
    assert service.generate(school.CHILD, 'weekly', earlier)['content']['learning_summary']['qualified_observations'] == []
    with monkeypatch.context() as loss:
        loss.setattr(RubricEvidenceService, 'calibrated_receipt', lambda *args, **kwargs: None)
        assert service.get(before['period_report_id'])['content']['learning_summary']['qualified_observations'] == []
    assert service.get(before['period_report_id'])['content']['learning_summary']['qualified_observations']
    url = f"/api/parent/children/{school.CHILD}/curriculum-supply/{baseline['candidate_id']}/withdraw"
    withdrawn = client.post(url, headers=headers, json=dict(action_id='period-source-withdraw',
        expected_version=baseline['version'], note='原题来源需要重新核对'))
    assert withdrawn.status_code == 200, withdrawn.text
    assert service.get(before['period_report_id'])['content']['learning_summary']['qualified_observations'] == []


def test_period_observation_does_not_hide_sqlite_failure(monkeypatch):
    import sqlite3
    from home_study_agent import period_written_observations as observations
    class BrokenConnection:
        def execute(self, *args):
            raise sqlite3.OperationalError('synthetic storage failure')
    with pytest.raises(sqlite3.OperationalError, match='synthetic storage failure'):
        observations.qualified_written_observations(BrokenConnection(), school.CHILD, [],
            now=datetime.now(timezone.utc))


def test_original_only_refresh_does_not_require_a_finished_daily_report(report_case, monkeypatch):
    db = report_case[0]
    authority(monkeypatch, report_case, 0)
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM daily_report').fetchone()[0] == 0
    result = PeriodReportService(db).refresh_current(school.CHILD)
    assert result['failures'] == []
    assert {r['report_type'] for r in result['reports']} == {'weekly', 'monthly'}
    assert all(len(r['content']['learning_summary']['qualified_observations']) == 1 for r in result['reports'])


def test_approved_original_snapshot_rechecks_authority_without_daily_or_rebuild(report_case, monkeypatch):
    from home_study_agent.learning_rubric import RubricEvidenceService
    from home_study_agent.parent_reports import ParentReportService
    db, client, headers = report_case[:3]
    authority(monkeypatch, report_case, 0)
    period = generate(client, headers, 'weekly')
    parents = ParentReportService(db)
    snapshot = parents.create_from_period(period)
    snapshot_id = snapshot['snapshot_id']
    parents.edit_draft(snapshot_id, parent_note='先听孩子说原来怎么想，不追加练习')
    parents.approve(snapshot_id)
    client = client.school_reopen()
    parents = ParentReportService(db)
    with db.connect() as c:
        frozen = dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot_id,)).fetchone())
        assert parents.snapshot_source_current(c, frozen)
    with monkeypatch.context() as loss:
        loss.setattr(RubricEvidenceService, 'calibrated_receipt', lambda *args, **kwargs: None)
        with db.connect() as c:
            before_changes = c.total_changes
            assert not parents.snapshot_source_current(c, frozen)
            assert c.total_changes == before_changes
        assert not parents.validate_current_for_delivery(snapshot_id)
    with db.connect() as c:
        current = dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot_id,)).fetchone())
        for field in ('facts_json', 'content_json', 'parent_edit_json', 'approved_at', 'approved_by'):
            assert current[field] == frozen[field]


@pytest.mark.parametrize('passed', [False, True])
def test_original_observation_personalizes_parent_coaching_without_mastery(passed):
    summary = PeriodReportService._learning_summary([], [], [])
    summary['qualified_observations'] = [dict(evidence_ref='record-synthetic', subject='数学',
        knowledge_title='分数的意义', date='2026-09-10', passed=passed, mastery_claim_allowed=False)]
    coaching = PeriodReportService._period_coaching(school.CHILD, {'grade': '四年级'}, summary, [])
    assert coaching['focus'] == 'period_review'
    assert coaching['personalization']['focus_title'] == '分数的意义'
    assert coaching['evidence_refs'] == ['record-synthetic']
    assert '缺少可追溯知识证据' not in coaching['why_this_fits']
    assert '分数的意义' in coaching['script'][0]
    assert '不等同掌握' in coaching['script'][2]
    assert ('达到本题量表要求' if passed else '还有一个步骤需要核对') in coaching['script'][2]
    assert coaching['action']['duration_minutes'] <= 8
    assert summary['coverage']['eligible_evidence_count'] == 0
    assert coaching['evidence_basis'][0] == '其他知识学习记录：0条'
    assert any('原作答：1份' in text for text in coaching['evidence_basis'])


@pytest.mark.parametrize('change', ['qualification_loss', 'human_correction'])
def test_stale_period_content_cannot_receive_a_new_authority_fingerprint(report_case, monkeypatch, change):
    from home_study_agent.learning_rubric import RubricEvidenceService
    from home_study_agent.parent_reports import ParentReportService, SnapshotStateError
    db, client, headers = report_case[:3]
    _, _, _, (path, body) = authority(monkeypatch, report_case, 0)
    old_period = generate(client, headers, 'weekly')
    parents = ParentReportService(db)
    old_snapshot = parents.create_from_period(old_period)
    parents.edit_draft(old_snapshot['snapshot_id'], parent_note='保留已经核对过的家长意见')
    with db.connect() as c:
        frozen = dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?',
                               (old_snapshot['snapshot_id'],)).fetchone())
        count = c.execute('SELECT count(*) FROM parent_report_snapshot').fetchone()[0]
    if change == 'qualification_loss':
        monkeypatch.setattr(RubricEvidenceService, 'calibrated_receipt', lambda *args, **kwargs: None)
    else:
        response = client.post(path, headers=headers, json=body)
        assert response.status_code == 200, response.text
    with pytest.raises(SnapshotStateError):
        parents.create_from_period(old_period)
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM parent_report_snapshot').fetchone()[0] == count
        assert dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?',
                              (old_snapshot['snapshot_id'],)).fetchone()) == frozen
    fresh = PeriodReportService(db).get(old_period['period_report_id'], persist_refresh=True)
    renewed = parents.create_from_period(fresh)
    assert renewed['snapshot_id'] != old_snapshot['snapshot_id']
    with db.connect() as c:
        row = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (renewed['snapshot_id'],)).fetchone()
        assert parents.snapshot_source_current(c, row)
