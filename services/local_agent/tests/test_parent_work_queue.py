import json

import pytest

from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.local_ocr.review_queue import OcrReviewQueue
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.report_delivery import ReportDeliveryService
from home_study_agent.variant_supply import VariantSupplyService
from home_study_agent.parent_work_queue import ParentWorkQueueService, WorkQueueConflict
from test_curriculum_supply import prepared, draft, CHILD

OTHER = 'child-junior-3'


def ocr_items(queue, child, count):
    rows = []
    for i in range(count):
        rows += queue.prepare({'artifact_id': 'isolated-'+child+'-'+str(i), 'details': {
            'lines': [{'text': '合成待校对片段 '+str(i), 'confidence': .2}],
            'local_pipeline': {'manual_review': {'required': True, 'reasons': ['low_ocr_confidence']}}}},
            child, None, 'prepare-'+child+'-'+str(i))['items']
    return rows


@pytest.fixture
def work(tmp_path):
    db, curriculum, payload = prepared(tmp_path)
    DiagnosticPromptService(db)
    VariantSupplyService(db).ensure_schema()
    RubricEvidenceService(db).initialize()
    ParentReportService(db).initialize()
    ReportDeliveryService(db, b'D'*32).initialize()
    ocr = OcrReviewQueue(db)
    return db, curriculum, payload, ocr, ParentWorkQueueService(db)


def test_true_totals_pagination_and_existing_ocr_undo_after_restart(work):
    db, _, _, ocr, service = work
    rows = ocr_items(ocr, CHILD, 43)
    ocr_items(ocr, OTHER, 3)
    first = service.list(limit=7)
    assert next(s['total'] for s in first['sources'] if s['child_id']==CHILD and s['kind']=='ocr') == 43
    ids = [r['work_id'] for r in first['items']]
    page = first
    while page['next_cursor']:
        page = service.list(limit=7, cursor=page['next_cursor'])
        ids += [r['work_id'] for r in page['items']]
    assert len(ids) == len(set(ids)) == first['total']
    assert all(s['status']=='ready' for s in first['sources'])
    ocr.batch([r['review_id'] for r in rows[:2]], CHILD, 'skip', 'real-batch-skip')
    with pytest.raises(WorkQueueConflict):
        service.list(limit=7, cursor=first['next_cursor'])
    current = ParentWorkQueueService(Database(db.path)).list(child_id=CHILD)
    assert next(s['total'] for s in current['sources'] if s['kind']=='ocr') == 41
    ocr.undo(rows[0]['review_id'], CHILD, 'real-undo')
    assert next(s['total'] for s in service.list(child_id=CHILD)['sources'] if s['kind']=='ocr') == 42


def test_parent_decision_audience_excludes_subject_review_and_reports_system_backlog(work):
    _, _, _, ocr, service = work
    ocr_items(ocr, CHILD, 3)
    result = service.list(child_id=CHILD, audience='parent_decisions')
    assert result['items'] == []
    assert result['total'] == 0 and result['known_total'] == 0
    assert result['system_verification_total'] >= 3
    assert {source['kind'] for source in result['sources']} == {'safety', 'delivery_unknown'}


def test_unavailable_source_is_not_zero_and_read_has_no_schema_side_effect(work):
    db, _, _, ocr, service = work
    ocr_items(ocr, CHILD, 1)
    with db.connect() as c:
        c.execute('ALTER TABLE rubric_review_event RENAME TO unavailable_rubric_review_event')
        c.commit()
        before = c.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()[0]
    result = service.list(child_id=CHILD)
    source = next(s for s in result['sources'] if s['kind']=='rubric')
    assert source == {'child_id': CHILD, 'kind': 'rubric', 'status': 'unavailable', 'total': None, 'error_code': 'WORK_SOURCE_UNAVAILABLE'}
    assert result['total'] is None and result['known_total'] > 0
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()[0] == before
        assert c.execute("SELECT status FROM ocr_review_item").fetchone()[0] == 'pending'


def test_curriculum_real_review_and_withdrawal_projection_is_read_only(work):
    db, curriculum, payload, _, service = work
    item = draft(curriculum, payload)
    result = service.list(child_id=CHILD)
    matches = [r for r in result['items'] if r['object_id']==item['candidate_id']]
    assert len(matches)==1 and matches[0]['kind']=='curriculum'
    assert matches[0]['target']['object_id']==item['candidate_id']
    assert any('125' in e['text'] for e in matches[0]['evidence'])
    assert not any(r['kind']=='specialized' and r['object_id']==item['candidate_id'] for r in result['items'])
    checked = curriculum.validate(CHILD, item['candidate_id'], actor='validator', action_id='check', expected_version=1)
    curriculum.review(CHILD, item['candidate_id'], actor='parent', decision='approve', note='隔离核对', action_id='approve', expected_version=checked['version'])
    curriculum.publish(CHILD, item['candidate_id'], actor='parent', action_id='publish', expected_version=3)
    assert item['candidate_id'] not in [r['object_id'] for r in service.list(child_id=CHILD)['items']]
    curriculum.withdraw(CHILD, item['candidate_id'], actor='parent', note='重新核对', action_id='withdraw', expected_version=4)
    assert item['candidate_id'] in [r['object_id'] for r in service.list(child_id=CHILD)['items']]
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        before = c.total_changes
        service.list(child_id=CHILD, connection=c)
        assert c.total_changes == before
