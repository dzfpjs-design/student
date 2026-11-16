import json
from uuid import uuid4

import pytest

from home_study_agent.diagnostic_prompt import _TransactionDatabase
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.mistake_plan_completion import active_binding, settle
from home_study_agent.repository import StudyRepository
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.mistake_dialogue import MistakeDialogueService
from home_study_agent.mistake_scored_correction import (
    finalize,
    finalize_withdrawal,
    scored_binding,
)
from test_mistake_gate_reliability import CHILD, SECOND, flow, prepare
from test_mistake_report_plan_projection import plan


@pytest.fixture
def review(flow):
    db, _, _, _ = flow
    book, case = prepare(flow)
    item = plan(flow, case)[0]
    repo = StudyRepository(db)
    with db.connect() as c:
        sid = c.execute('SELECT session_id FROM daily_plan WHERE plan_id=(SELECT plan_id FROM plan_item WHERE plan_item_id=?)',
                        (item['plan_item_id'],)).fetchone()[0]
    repo.transition(sid, 'plan_review', 'study', 'test review', str(uuid4()))
    repo.start_plan_item(item['plan_item_id'], str(uuid4()))
    focus = {'session_id': sid, 'child_id': CHILD, 'mistake_id': case}
    with db.connect() as c:
        operation = {'kind': 'mastery_answer', 'action_id': str(uuid4()), 'stage': 'near_transfer',
                     'item_id': SECOND, 'answer': '42', 'hint_level': 0,
                     'plan_item_binding': active_binding(c, focus)}
    assert operation['plan_item_binding']
    return db, repo, book, item, focus, operation


def save_answer(c, focus, operation):
    book = MistakeBookService(_TransactionDatabase(c))
    book.submit_attempt(focus['mistake_id'], focus['child_id'], operation['stage'], operation['item_id'],
                        operation['answer'], operation['hint_level'], operation['action_id'] + ':attempt',
                        _session_id=focus['session_id'])
    return book


def test_success_settles_only_today_and_replay_has_one_receipt(review):
    db, repo, book, item, focus, op = review
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        scoped_book = save_answer(c, focus, op)
        receipt = settle(c, focus, op, scoped_book)
        assert receipt['next_activity']['kind'] == 'scheduled'
        assert settle(c, focus, op, scoped_book) == receipt
        c.commit()
    assert repo._get_plan_item_by_id(item['plan_item_id'])['status'] == 'completed'
    assert book.get(focus['mistake_id'], CHILD)['status'] != 'mastered'
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='completed'").fetchone()[0] == 1
        payload = json.loads(c.execute("SELECT payload_json FROM plan_item_event WHERE event_type='completed'").fetchone()[0])
        assert payload['mistake_completion']['evidence_id'] == receipt['evidence_id']


def test_correcting_completed_review_reopens_task_and_withdraws_old_completion(review):
    db, repo, _, item, focus, op = review
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        scoped_book = save_answer(c, focus, op)
        receipt = settle(c, focus, op, scoped_book)
        c.commit()
    assert repo._get_plan_item_by_id(item['plan_item_id'])['status'] == 'completed'

    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        binding = scored_binding(c, focus, op)
        correction = {**op, 'action_id': 'correct-completed-review',
                      'utterance': '刚才答案改成17', 'answer': '17',
                      'correction_of': binding}
        scoped_book = MistakeBookService(_TransactionDatabase(c))
        scoped_book.submit_attempt(focus['mistake_id'], focus['child_id'], op['stage'],
            op['item_id'], '17', op['hint_level'], correction['action_id'] + ':attempt',
            _session_id=focus['session_id'], _correction_of_attempt_id=binding['attempt_id'])
        new_evidence = c.execute('SELECT evidence_id FROM mistake_attempt WHERE client_action_id=?',
            (correction['action_id'] + ':attempt',)).fetchone()[0]
        result = finalize(c, focus, correction, new_evidence, scoped_book)
        c.commit()

    assert result['reopened_plan_item_ids'] == [item['plan_item_id']]
    restored = repo._get_plan_item_by_id(item['plan_item_id'])
    assert restored['status'] == 'rescheduled'
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='completed'").fetchone()[0] == 1
        event = c.execute("SELECT payload_json FROM plan_item_event WHERE event_type='source_withdrawn'").fetchone()
        assert json.loads(event[0])['old_evidence_id'] == receipt['evidence_id']


def test_withdrawing_completed_review_reopens_task_without_replacement(review):
    db, repo, _, item, focus, op = review
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        scoped_book = save_answer(c, focus, op)
        receipt = settle(c, focus, op, scoped_book)
        c.commit()
    assert repo._get_plan_item_by_id(item['plan_item_id'])['status'] == 'completed'

    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        binding = scored_binding(c, focus, op)
        withdrawal = {'kind': 'scored_withdrawal',
            'action_id': 'withdraw-completed-review',
            'utterance': '刚才的答案不算', 'withdrawal_of': binding}
        result = finalize_withdrawal(c, focus, withdrawal,
            MistakeBookService(_TransactionDatabase(c)))
        c.commit()

    assert result['replacement_evidence_id'] is None
    assert result['reopened_plan_item_ids'] == [item['plan_item_id']]
    assert repo._get_plan_item_by_id(item['plan_item_id'])['status'] == 'rescheduled'
    with db.connect() as c:
        row = c.execute('''SELECT reason,replacement_evidence_id FROM learning_evidence_withdrawal
            WHERE evidence_id=?''', (receipt['evidence_id'],)).fetchone()
        assert tuple(row) == ('answer_withdrawn', None)
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='completed'").fetchone()[0] == 1
        event = c.execute("SELECT payload_json FROM plan_item_event WHERE event_type='source_withdrawn'").fetchone()
        payload = json.loads(event[0])
        assert payload['old_evidence_id'] == receipt['evidence_id']
        assert payload['replacement_evidence_id'] is None


@pytest.mark.parametrize('change', ['unbound', 'other_child', 'other_case', 'paused', 'new_start', 'inactive_plan',
                                  'wrong', 'hinted', 'low_confidence'])
def test_unqualified_or_changed_work_cannot_settle(review, change):
    db, repo, _, item, focus, op = review
    if change == 'unbound':
        op.pop('plan_item_binding')
    elif change in {'other_child', 'other_case'}:
        op['plan_item_binding']['child_id' if change == 'other_child' else 'source_ref'] = 'different-owner'
    elif change == 'wrong':
        op['answer'] = '17'
    elif change == 'hinted':
        op['hint_level'] = 1
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        scoped_book = save_answer(c, focus, op)
        if change == 'paused':
            c.execute("UPDATE plan_item SET status='paused' WHERE plan_item_id=?", (item['plan_item_id'],))
        elif change == 'new_start':
            c.execute("UPDATE plan_item SET started_at='2099-01-01T00:00:00+00:00' WHERE plan_item_id=?", (item['plan_item_id'],))
        elif change == 'inactive_plan':
            c.execute("UPDATE daily_plan SET status='superseded' WHERE plan_id=?", (op['plan_item_binding']['plan_id'],))
        elif change == 'low_confidence':
            c.execute('UPDATE learning_evidence SET confidence=0 WHERE evidence_id IN (SELECT evidence_id FROM mistake_attempt)')
        assert settle(c, focus, op, scoped_book) is None
        assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='completed'").fetchone()[0] == 0
        c.commit()
    assert repo._get_plan_item_by_id(item['plan_item_id'])['status'] != 'completed'


def test_storage_failure_rolls_back_answer_and_task_then_recovers(review, monkeypatch):
    db, repo, _, item, focus, op = review
    original = StudyRepository.complete_plan_item
    def interrupted(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError('synthetic interruption before transaction commit')
    with monkeypatch.context() as patch:
        patch.setattr(StudyRepository, 'complete_plan_item', interrupted)
        with pytest.raises(RuntimeError, match='synthetic interruption'):
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                scoped_book = save_answer(c, focus, op)
                settle(c, focus, op, scoped_book)
                c.commit()
    assert repo._get_plan_item_by_id(item['plan_item_id'])['status'] == 'active'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='completed'").fetchone()[0] == 0
        c.execute('BEGIN IMMEDIATE')
        scoped_book = save_answer(c, focus, op)
        assert settle(c, focus, op, scoped_book)
        c.commit()
    assert repo._get_plan_item_by_id(item['plan_item_id'])['status'] == 'completed'


@pytest.mark.parametrize('change', ['unchanged', 'pause', 'new_start', 'other_source'])
def test_voice_answer_fingerprint_includes_the_original_daily_task(review, change):
    db, _, _, item, focus, _ = review
    dialogue = MistakeDialogueService(db)
    opened = dialogue.open_review(focus['session_id'], 'open-bound-review')
    assert opened['phase'] in {'attempt', 'diagnosis_probe'}
    with db.connect() as c:
        before = AgentRuntimeStore.local_mistake_answer_target(c, focus['session_id'], CHILD)
        assert before is not None
        if change == 'pause':
            c.execute("UPDATE plan_item SET status='paused' WHERE plan_item_id=?", (item['plan_item_id'],))
        elif change == 'new_start':
            c.execute("UPDATE plan_item SET started_at='2099-01-01T00:00:00+00:00' WHERE plan_item_id=?", (item['plan_item_id'],))
        elif change == 'other_source':
            c.execute("UPDATE plan_item SET source_ref='different-mistake' WHERE plan_item_id=?", (item['plan_item_id'],))
        after = AgentRuntimeStore.local_mistake_answer_target(c, focus['session_id'], CHILD)
        assert (before == after) is (change == 'unchanged')
