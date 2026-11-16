"""Real TCP/SQLite consumers; injected admission only, never a calibration run."""
from types import SimpleNamespace

import pytest

from home_study_agent.learning_rubric import RubricEvidenceService
import test_school_open_reviews as school
from test_school_review_start_integrity_http import _http_client
from test_written_score_corrupt_history import corrupt_history
from test_writing_calibrated_gate import authority


@pytest.fixture
def consumer_case(tmp_path, monkeypatch):
    monkeypatch.setattr(school, 'TestClient', _http_client)
    yield from school.school_case.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param='public'))


def read(client, path, headers):
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def work(client, headers):
    return read(client, '/v1/parent/work-queue?child_id=' + school.CHILD + '&limit=50', headers)


ROOT = f'/v1/parent/children/{school.CHILD}/learning-rubrics'


@pytest.mark.parametrize('score', [0, 3])
def test_current_qualification_queue_and_restart(consumer_case, monkeypatch, score):
    db, client, headers = consumer_case[:3]
    evidence, receipt, _, (review_path, body) = authority(monkeypatch, consumer_case, score)
    aid = receipt['assessment_id']
    before_calls = len(client.school_gateway.requests)
    initial = read(client, ROOT + '/' + aid, headers)
    assert initial['assessment']['score_status'] == 'candidate'
    assert initial['qualification']['score_qualified'] is True
    assert initial['qualification']['receipt']['score_status'] == 'calibrated_model'
    assert initial['qualification']['eligible_for_mastery_gate'] is (score == 3)
    assert initial['qualification']['automatic_certification'] is False
    page = work(client, headers)
    assert next(s for s in page['sources'] if s['kind'] == 'rubric')['total'] == 0
    assert not any(item['kind'] == 'rubric' for item in page['items'])
    client = client.school_reopen()
    headers = {'X-HSC-Parent': client.post('/v1/parent/unlock', json={'pin': '8264'}).json()['parent_token']}
    listed = read(client, ROOT, headers)['items'][0]
    assert listed['score_status'] == 'candidate'
    assert listed['qualification'] == initial['qualification']
    assert read(client, ROOT + '/' + aid, headers)['qualification'] == initial['qualification']
    reopened_page = work(client, headers)
    # Startup now creates weekly/monthly drafts from qualified originals even
    # without a daily report. Those real new tasks must change the queue version.
    assert [s for s in reopened_page['sources'] if s['kind'] != 'report_review'] == [
        s for s in page['sources'] if s['kind'] != 'report_review']
    assert [r for r in reopened_page['items'] if r['kind'] != 'report_review'] == [
        r for r in page['items'] if r['kind'] != 'report_review']
    with db.connect() as c:
        reports = c.execute('SELECT snapshot_id,report_type FROM parent_report_snapshot WHERE child_id=?',
            (school.CHILD,)).fetchall()
        assert {r['report_type'] for r in reports} == {'weekly', 'monthly'}
        assert {r['snapshot_id'] for r in reports} == {
            r['object_id'] for r in reopened_page['items'] if r['kind'] == 'report_review'}
    assert reopened_page['revision'] != page['revision']
    assert work(client, headers)['revision'] == reopened_page['revision']
    with db.connect() as c:
        assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE evidence_id=?', (evidence,)).fetchone()[0] is None
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event WHERE assessment_id=?', (aid,)).fetchone()[0] == 0

    # Simulate current admission disappearing; do not claim a real revocation transaction.
    monkeypatch.setattr(RubricEvidenceService, 'calibrated_receipt', lambda *a, **kw: None)
    client = client.school_reopen()
    headers = {'X-HSC-Parent': client.post('/v1/parent/unlock', json={'pin': '8264'}).json()['parent_token']}
    expired = read(client, ROOT, headers)['items'][0]
    assert expired['score_status'] == 'candidate'
    assert expired['qualification']['score_qualified'] is False
    fallback = work(client, headers)
    assert any(item['kind'] == 'rubric' and item['object_id'] == aid for item in fallback['items'])
    assert fallback['revision'] != page['revision']

    approved = client.post(review_path, headers=headers, json=body)
    assert approved.status_code == 200, approved.text
    assert not any(item['kind'] == 'rubric' for item in work(client, headers)['items'])
    withdrawn = client.post(review_path, headers=headers, json={
        'operation': 'retract', 'expected_revision': 1, 'client_action_id': 'consumer-withdraw', 'reason': 'Synthetic parent correction'})
    assert withdrawn.status_code == 200, withdrawn.text
    assert any(item['kind'] == 'rubric' and item['object_id'] == aid for item in work(client, headers)['items'])
    assert len(client.school_gateway.requests) == before_calls


def test_qualification_reads_are_page_bounded_and_child_scoped(consumer_case, monkeypatch):
    db, client, headers = consumer_case[:3]
    _, target, _, _ = authority(monkeypatch, consumer_case, 0)
    for number in range(3):
        school.answer_and_review(consumer_case, key=f'other-{number}', submit=False)
    _, _, foreign, _, _ = school.answer_and_review(consumer_case, key='foreign', submit=False)
    with db.connect() as c:
        row = c.execute('SELECT step_id,payload_json FROM rubric_assessment WHERE assessment_id=?', (target['assessment_id'],)).fetchone()
        step = row['step_id']
        foreign_payload = c.execute('SELECT payload_json FROM rubric_assessment WHERE assessment_id=?', (foreign['assessment_id'],)).fetchone()[0]
        corrupt_history(c, foreign['assessment_id'], foreign_payload, 'child-primary-4')
        c.commit()
    provider = RubricEvidenceService.calibrated_receipt
    monkeypatch.setattr(RubricEvidenceService, 'calibrated_receipt',
        lambda self, c, child, current_step, **kw: provider(self, c, child, current_step, **kw) if current_step == step else None)
    original = RubricEvidenceService.qualification
    calls = []

    def observe(self, child, assessment_id, *, connection=None):
        assert child == school.CHILD and connection is not None and connection.in_transaction
        changes = connection.total_changes
        calls.append(assessment_id)
        result = original(self, child, assessment_id, connection=connection)
        assert connection.total_changes == changes
        return result

    monkeypatch.setattr(RubricEvidenceService, 'qualification', observe)
    before_calls = len(client.school_gateway.requests)
    listed = read(client, ROOT + '?limit=1&offset=0', headers)
    assert listed['total'] == 4 and len(listed['items']) == 1
    assert len(calls) == 1 and calls[0] == listed['items'][0]['assessment_id']
    calls.clear()
    page = work(client, headers)
    assert len(calls) == len(set(calls)) == 4
    assert foreign['assessment_id'] not in calls
    assert next(s for s in page['sources'] if s['kind'] == 'rubric')['total'] == 3
    assert all(item['object_id'] != target['assessment_id'] for item in page['items'] if item['kind'] == 'rubric')
    for path in (ROOT + '/' + foreign['assessment_id'],
                 '/v1/parent/children/child-primary-4/learning-rubrics/' + target['assessment_id']):
        rejected = client.get(path, headers=headers)
        assert rejected.status_code == 409, rejected.text
        assert 'qualification' not in rejected.json()
    assert len(client.school_gateway.requests) == before_calls


@pytest.mark.parametrize('bad', ['{', '{"source_evidence_id":"missing"}'])
def test_corrupt_current_history_is_not_a_qualified_empty_queue(consumer_case, monkeypatch, bad):
    db, client, headers = consumer_case[:3]
    _, target, _, _ = authority(monkeypatch, consumer_case, 3)
    with db.connect() as c:
        corrupt_history(c, target['assessment_id'], bad)
        c.commit()
    # Exercise live consumer reads. Corrupt-journal startup replay is a separate
    # red boundary in evaluator recovery, outside this consumer-only repair.
    assert client.get(ROOT, headers=headers).status_code == 409
    page = work(client, headers)
    source = next(s for s in page['sources'] if s['kind'] == 'rubric')
    assert source['status'] == 'unavailable' and source['total'] is None
    assert source['error_code'] == 'WORK_SOURCE_UNAVAILABLE' and page['total'] is None
