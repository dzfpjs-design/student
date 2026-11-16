import hashlib
import json
from datetime import timedelta
import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.mistake_learning import MistakeLearningService
from home_study_agent.mistake_learning_http import create_router
from home_study_agent.mistake_learning_intake import MistakeLearningIntake
from home_study_agent.mistake_print import snapshot
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import StudyRepository


def digest(db):
    with db.connect() as connection:
        return hashlib.sha256('\n'.join(connection.iterdump()).encode()).hexdigest()


def test_print_http_complete_owned_snapshot_is_read_only_and_survives_restart(tmp_path):
    db = Database(tmp_path / 'print.db')
    db.initialize()
    PrivacyLifecycleService(db, tmp_path).initialize()
    repository = StudyRepository(db)
    repository.seed_family()
    child = 'child-primary-4'
    session = repository.start_session(child, 'print-session')['session_id']
    intake = MistakeLearningIntake(db)
    card = intake.create(child_id=child, question='计算3+4。', submitted_answer='八',
                         subject='数学', client_action_id='print-card')
    other = intake.create(child_id='child-junior-3', question='另一孩子的题目不应出现', client_action_id='other-card')
    service = MistakeLearningService(db, None)
    mid = card['mistake_id']
    for action, answer in [('wrong', '八'), ('correct', '七'), ('uncertain', '不知道')]:
        result = service.review(mid, child_id=child, session_id=session,
                                client_action_id=action, modality='voice', utterance=answer)
    # Replaying an action is a saved receipt, never an additional review.
    service.review(mid, child_id=child, session_id=session,
                   client_action_id='correct', modality='voice', utterance='七')
    with db.connect() as c:
        c.execute('UPDATE mistake_learning_state SET analysis_json=? WHERE mistake_id=?',
                  (json.dumps({'summary': '核对计算过程', 'wrong_causes': [{'label': '加法计算', 'explanation': '原答与计算结果不同'}],
                               'knowledge_points': [], 'correction_strategy': []}), mid))
        # Durable answer receipts from different practice batches are included;
        # coach, failed, and processing actions do not inflate review counts.
        original = dict(c.execute("SELECT * FROM mistake_learning_action WHERE client_action_id='correct'").fetchone())
        for key, operation, status in [('practice-1', 'answer', 'completed'), ('practice-2', 'answer', 'completed'),
                                       ('coach', 'coach', 'completed'), ('failed', 'answer', 'failed')]:
            fields = list(original)
            value = original | {'client_action_id': key, 'operation': operation, 'status': status}
            c.execute('INSERT INTO mistake_learning_action (' + ','.join(fields) + ') VALUES (' + ','.join('?' for _ in fields) + ')',
                      [value[field] for field in fields])
        c.commit()
    for index in range(105):
        intake.create(child_id=child, question=f'复习题{index}：请说明思路。', subject='语文', client_action_id=f'bulk-{index}')

    class ReadOnlyMatcher:
        def cache_version(self, *, connection=None):
            assert connection is not None and connection.in_transaction
            return 'current-graph'

        def match(self, *args):
            raise AssertionError('PRINT_MUST_NOT_REFRESH_GRAPH')

    service.graph_matcher = ReadOnlyMatcher()
    app = FastAPI()
    app.include_router(create_router(service, authorize_request=lambda req: req.headers.get('X-Test') == 'yes'))
    before = digest(db)
    with TestClient(app) as client:
        path = f'/v1/children/{child}/mistake-learning/print'
        assert client.get(path).status_code == 403
        response = client.get(path, headers={'X-Test': 'yes'})
        assert response.status_code == 200, response.text
        assert response.headers['cache-control'] == 'no-store'
        result = response.json()
        assert result['total'] == len(result['items']) == 106
        assert all(item['record']['child_id'] == child for item in result['items'])
        assert other['mistake_id'] not in {item['record']['mistake_id'] for item in result['items']}
        item = next(item for item in result['items'] if item['record']['mistake_id'] == mid)
        assert item['review_count'] == 4
        assert item['review_counts'] == {'original': 2, 'practice': 2, 'formal': 0}
        assert item['mastery']['status'] == 'retest_pending'
        assert item['last_review_at']
        assert item['record']['analysis']['wrong_causes'][0]['label'] == '加法计算'
        assert client.get('/v1/children/unknown/mistake-learning/print', headers={'X-Test': 'yes'}).status_code == 404
    assert digest(db) == before
    reopened = MistakeLearningService(Database(db.path), None)
    assert snapshot(reopened, child)['items'] == result['items']
    assert digest(db) == before
    with db.connect() as c:
        c.execute('UPDATE mistake_learning_card SET prompt_summary=? WHERE mistake_id=?', ('计算4+5。', mid))
        c.commit()
    after_correction = digest(db)
    changed = next(item for item in snapshot(reopened, child)['items'] if item['record']['mistake_id'] == mid)
    assert changed['record']['source']['question'] == '计算4+5。'
    assert changed['review_count'] == 0 and changed['mastery']['status'] == 'unassessed'
    assert changed['record']['analysis'] is None
    assert digest(db) == after_correction


def test_formal_case_uses_current_mastery_evidence_and_legacy_diagnosis(tmp_path):
    db = Database(tmp_path / 'formal-print.db')
    db.initialize()
    PrivacyLifecycleService(db, tmp_path).initialize()
    StudyRepository(db).seed_family()
    LearningStateService(db).seed_catalog()
    child = 'child-junior-3'
    book = MistakeBookService(db)
    card = book.capture(child_id=child, knowledge_node_id='node-j3-math-discriminant', subject='数学',
                        title='判别式复习', prompt_summary='方程 x²-4x+4=0 的判别式是多少？',
                        submitted_answer='4', source_type='diagnostic', source_ref='diag-j3-discriminant-1',
                        confidence=.6, client_action_id='formal-capture', diagnostic_item_id='diag-j3-discriminant-1')
    mid = card['mistake_id']
    book.confirm(mid, child, 'confirm', 'formal-confirm')
    book.diagnose(mid, child, 'execution_error', '平方与符号需要进一步核对。', .8, 'formal-diagnosis')
    service = MistakeLearningService(db, None)
    with db.connect() as c:
        c.execute("UPDATE mistake_case SET status='mastered' WHERE mistake_id=?", (mid,))
        c.commit()
    before = digest(db)
    item, = snapshot(service, child)['items']
    assert item['record']['source']['question'] == card['prompt_summary']
    assert item['diagnosis'] == {'label': '执行计算', 'explanation': '平方与符号需要进一步核对。', 'status': 'candidate'}
    assert item['mastery']['status'] != 'mastered'
    assert item['review_count'] == 0
    assert digest(db) == before
    with db.connect() as c:
        c.execute("UPDATE mistake_case SET status='excluded' WHERE mistake_id=?", (mid,))
        c.commit()
    assert snapshot(service, child)['total'] == 0


def test_print_mastery_follows_real_formal_reviews_and_evidence_withdrawal(tmp_path, monkeypatch):
    from test_mistake_gate_reliability import CHILD, NODE, build_flow, prepare, submit
    from home_study_agent import mistake_print

    flow = build_flow(tmp_path, monkeypatch)
    db, _, _, clock = flow
    PrivacyLifecycleService(db, tmp_path).initialize()
    monkeypatch.setattr(mistake_print, '_now', lambda: clock[0].isoformat())
    service = MistakeLearningService(db, None)
    book, mid = prepare(flow)
    submit(book, mid)
    clock[0] += timedelta(hours=49)
    assert submit(book, mid, 'delayed_48h')['status'] == 'mastered'
    item, = snapshot(service, CHILD)['items']
    assert item['mastery']['status'] == 'mastered'
    assert item['review_counts'] == {'original': 0, 'practice': 0, 'formal': 2}
    with db.connect() as c:
        evidence = c.execute("SELECT evidence_id FROM mistake_attempt WHERE mistake_id=? AND stage='near_transfer'", (mid,)).fetchone()[0]
    withdrawn = book.capture(child_id=CHILD, knowledge_node_id=NODE, subject='Math', title='Correction',
                             prompt_summary='Withdraw this evidence', submitted_answer='wrong', source_type='manual',
                             source_ref=None, confidence=1, client_action_id='print-withdrawal-case', source_evidence_id=evidence)
    book.confirm(withdrawn['mistake_id'], CHILD, 'exclude', 'print-withdrawal')
    before = digest(db)
    updated, = snapshot(service, CHILD)['items']
    assert updated['mastery']['status'] != 'mastered'
    assert digest(db) == before


@pytest.mark.parametrize('old_status', ['fragile', 'delayed_verification', 'relapsed', 'mastered'])
def test_new_question_does_not_inherit_older_formal_mastery_labels(old_status):
    from home_study_agent.mistake_print import _mastery
    record = {'original_review': {'status': 'not_started', 'last_result': None}}
    old_detail = {'status': old_status, 'mastery_gate': {'missing': []}}
    old_time, changed = '2026-11-01T10:00:00+00:00', '2026-11-02T10:00:00+00:00'
    assert _mastery(record, old_detail, changed, old_time, old_time)['status'] == 'unassessed'
    record['original_review'] = {'status': 'retest_pending', 'last_result': {'created_at': changed}}
    assert _mastery(record, old_detail, changed, old_time, old_time)['status'] == 'retest_pending'
