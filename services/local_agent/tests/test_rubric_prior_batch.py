"""Batching is only a read optimization; current receipt authority is unchanged."""
from datetime import datetime, timedelta, timezone
import json
import random
import re
from time import perf_counter
import sqlite3

import pytest

from home_study_agent.goal_section_coverage import _rubric_receipts
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.rubric_prior_index import RubricPriorIndex, _EarlierAtTime
from test_learning_outcome_mastery_gate import gate, flow, SECOND
from test_learning_rubric_review import review
from test_rubric_prior_time_recovery import corrupt_prior
from test_writing_stage_reviews import writing_chain, reviewed_answer
from test_writing_response_origin import CHILD


def private_marker_pairs(statements):
    stack, seen, pairs = [], set(), 0
    for statement in statements:
        match = re.fullmatch(r'(SAVEPOINT|RELEASE SAVEPOINT) (hsc_original_read_[0-9a-f]{32})', statement)
        assert match is not None, statement
        operation, marker = match.groups()
        if operation == 'SAVEPOINT':
            assert marker not in seen, statement
            seen.add(marker)
            stack.append(marker)
        else:
            assert stack and stack.pop() == marker, statement
            pairs += 1
    assert not stack, statements
    return pairs


def test_section_prior_history_is_loaded_once_not_per_review(writing_chain, monkeypatch, record_property):
    db, sid, prompts, originals, _, near, _ = writing_chain
    text = ('Dear Ben, our school concert was in the hall on Friday. I enjoyed the violin performance '
            'because the music was cheerful. Would you like to come with me next time? Best wishes, Mia.')
    for n in range(5):
        reviewed_answer(db, sid, prompts, originals, near['diagnostic_item_id'], text, 'batch-original-' + str(n))
    with db.connect() as raw:
        raw.execute('BEGIN')
        raw.execute('PRAGMA query_only=ON')
        # Instrument fetched rows on the real original connection/cursors.
        # The connection factory and private read authorization stay unchanged.
        c = raw
        c.prior_rows = 0
        original_execute = raw.execute
        def counted_execute(sql, *args):
            cursor = original_execute(sql, *args)
            if sql.startswith(('SELECT step_id,payload_json FROM rubric_assessment',
                               'SELECT rowid AS _rowid,assessment_id,step_id,payload_json FROM rubric_assessment')):
                factory = cursor.row_factory
                assert factory is sqlite3.Row
                def counted_row(cursor, values):
                    c.prior_rows += 1
                    return factory(cursor, values)
                cursor.row_factory = counted_row
            return cursor
        attempts = {r['evidence_id']: dict(r) for r in raw.execute('SELECT * FROM diagnostic_attempt WHERE child_id=?', (CHILD,))}
        eligible = {r['diagnostic_item_id']: 'writing' for r in attempts.values()}
        count = raw.execute('SELECT COUNT(*) FROM rubric_assessment WHERE child_id=?', (CHILD,)).fetchone()[0]
        boundaries = []
        def trace(statement):
            command = statement.lstrip().split(None, 1)[0].upper()
            if command in {'BEGIN', 'COMMIT', 'END', 'ROLLBACK', 'SAVEPOINT', 'RELEASE'}:
                boundaries.append(statement)
        raw.set_trace_callback(trace)
        started = perf_counter()
        try:
            with monkeypatch.context() as instrumentation:
                instrumentation.setattr(raw, 'execute', counted_execute)
                receipts = _rubric_receipts(c, CHILD, eligible, attempts, datetime.now(timezone.utc))
        finally:
            raw.set_trace_callback(None)
        assert len(receipts) == count == 6
        assert raw.total_changes == 0
        # Public roadmap callers own BEGIN, but do not open a batch snapshot.
        # The current source validator must pair its private safety markers;
        # it must not commit, roll back, or replace this caller's transaction.
        assert raw.in_transaction
        marker_pairs = private_marker_pairs(boundaries)
        assert marker_pairs > 0
        assert c.prior_rows > 0
        record_property('history_rows', c.prior_rows)
        record_property('assessments', count)
        record_property('private_read_marker_pairs', marker_pairs)
        record_property('batch_elapsed_ms', round((perf_counter() - started) * 1000, 3))
        assert c.prior_rows <= count, {'history_rows': c.prior_rows, 'assessments': count}


def paired(db, step, at, child=CHILD):
    service = RubricEvidenceService(db)
    with db.connect() as c:
        c.execute('BEGIN')
        c.execute('PRAGMA query_only=ON')
        scalar = service.reviewed_receipt(c, child, step, now=at)
        with RubricPriorIndex(c, child) as index:
            batch = service.reviewed_receipt(c, child, step, now=at, _prior_index=index)
        assert batch == scalar
        assert c.total_changes == 0
        return batch


def test_range_index_preserves_exact_window_row_order_and_holes():
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rng = random.Random(4827)
    entries = [(now + timedelta(microseconds=rng.randrange(-200000000000, 200000000000)), row)
               for row in [-17, -1, 0, 2, 9, 50, 1000, 9000]]
    entries += [(now-timedelta(hours=24), 3), (now, 11), (now+timedelta(microseconds=1), 12)]
    index = _EarlierAtTime(list(entries))
    for occurred in [now, now+timedelta(microseconds=1), now-timedelta(microseconds=1)] + [at for at, _ in entries]:
        for rowid in [-18, -1, 0, 1, 3, 9, 11, 12, 500, 9001]:
            expected = any(row < rowid and timedelta(0) <= occurred-at < timedelta(hours=24)
                           for at, row in entries)
            assert index.before(rowid, occurred) == expected, (rowid, occurred)


def test_malformed_prefix_recovery_matches_every_earlier_record_without_truncation(monkeypatch):
    from home_study_agent import learning_rubric
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    limits = {'known_old': now-timedelta(days=1), 'future': now+timedelta(days=1), 'unknown': None}
    monkeypatch.setattr(learning_rubric, '_prior_answer_safe_after', lambda c, child, step: limits[step])
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.execute('CREATE TABLE rubric_assessment(assessment_id,child_id,step_id,payload_json)')
    c.execute('BEGIN')
    entries = [(-4, 'valid'), (0, 'known_old'), (7, 'valid'), (8, 'future'), (99, 'valid'),
               (500, 'unknown'), (1000, 'valid')]
    for rowid, kind in entries:
        payload = json.dumps({'question': 'Other identity', 'responded_at': now.isoformat()}) if kind == 'valid' else '{}'
        c.execute('INSERT INTO rubric_assessment(rowid,assessment_id,child_id,step_id,payload_json) VALUES(?,?,?,?,?)',
            (rowid, str(rowid), CHILD, kind, payload))
    # A foreign unresolved row must not enter this child's conservative prefix.
    c.execute("INSERT INTO rubric_assessment VALUES('foreign','other','unknown','{}')")
    with RubricPriorIndex(c, CHILD) as index:
        for rowid, _ in entries:
            for at in [now-timedelta(days=2), now, now+timedelta(days=1)]:
                expected = any(r < rowid and kind != 'valid' and (limits[kind] is None or at < limits[kind])
                               for r, kind in entries)
                assert index.assisted(c, CHILD, str(rowid), 'unrelated identity', at) == expected
    c.rollback()
    c.close()


@pytest.mark.parametrize('days,fault', [(.5, 'none'), (1, 'none'), (2, 'none'),
    (.5, 'broken'), (1, 'broken'), (2, 'broken'), (2, 'array'), (2, 'null'),
    (2, 'foreign'), (2, 'bad_arguments'), (2, 'time_mismatch')])
def test_original_time_and_optional_bad_history_are_differentially_equal(gate, days, fault):
    first, evidence, _ = gate.run(local=True)
    prior = first['evaluation']['rubric_assessment']
    current, _, _ = gate.run(local=True, item=SECOND, answer='42', days=days)
    snapshot = current['evaluation']['rubric_assessment']
    review(RubricEvidenceService(gate.db), snapshot)
    if fault != 'none':
        with gate.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            corrupt_prior(c, prior['assessment_id'], {'array': '[]', 'null': 'null'}.get(fault, '{broken'))
            if fault == 'foreign':
                c.execute("UPDATE agent_plan SET child_id='child-junior-3' WHERE plan_id=(SELECT plan_id FROM agent_step WHERE step_id=?)", (prior['step_id'],))
            elif fault == 'bad_arguments':
                c.execute("UPDATE agent_step SET arguments_json='[]' WHERE step_id=?", (prior['step_id'],))
            elif fault == 'time_mismatch':
                c.execute("UPDATE diagnostic_attempt SET created_at='2020-01-01T00:00:00+00:00' WHERE evidence_id=?", (evidence['evidence_id'],))
            c.commit()
    receipt = paired(gate.db, snapshot['step_id'], gate.clock[0])
    assert receipt and receipt['correctness'] == 1
    expected = fault == 'none' or days >= 1 and fault in {'broken', 'array', 'null'}
    assert receipt['independent'] is expected
    assert paired(gate.db, snapshot['step_id'], gate.clock[0], 'child-junior-3') is None


def test_index_scope_invalidated_on_write_exit_other_child_or_connection():
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.execute('CREATE TABLE rubric_assessment(assessment_id,child_id,step_id,payload_json)')
    c.execute('BEGIN')
    at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with RubricPriorIndex(c, CHILD) as index:
        with pytest.raises(RuntimeError, match='SCOPE_CHANGED'):
            index.assisted(c, 'other', 'absent', 'q', at)
        with sqlite3.connect(':memory:') as other:
            with pytest.raises(RuntimeError, match='SCOPE_CHANGED'):
                index.assisted(other, CHILD, 'absent', 'q', at)
        c.execute('INSERT INTO rubric_assessment VALUES(?,?,?,?)', ('a', CHILD, 's', '{}'))
        with pytest.raises(RuntimeError, match='SCOPE_CHANGED'):
            index.assisted(c, CHILD, 'a', 'q', at)
    with pytest.raises(RuntimeError, match='SCOPE_CHANGED'):
        index.assisted(c, CHILD, 'a', 'q', at)
    c.rollback()
    assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 0
    with pytest.raises(RuntimeError, match='TRANSACTION_REQUIRED'):
        RubricPriorIndex(c, CHILD)
    c.close()


def test_index_requires_active_context_and_finally_releases_loaded_history():
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.execute('CREATE TABLE rubric_assessment(assessment_id,child_id,step_id,payload_json)')
    at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    c.execute('INSERT INTO rubric_assessment VALUES(?,?,?,?)', ('a', CHILD, 's',
        json.dumps({'question': 'Original question', 'responded_at': at.isoformat()})))
    c.commit()
    c.execute('BEGIN')
    index = RubricPriorIndex(c, CHILD)
    with pytest.raises(RuntimeError, match='SCOPE_CHANGED'):
        index.assisted(c, CHILD, 'a', 'q', at)
    with pytest.raises(ValueError, match='render interrupted'):
        with index:
            assert not index.assisted(c, CHILD, 'a', 'q', at)
            assert index.rows
            raise ValueError('render interrupted')
    assert not index.active and index.rows is None and index.questions is None
    c.commit()
    c.execute('BEGIN')
    with pytest.raises(RuntimeError, match='SCOPE_CHANGED'):
        with index:
            pass
    with RubricPriorIndex(c, CHILD) as fresh:
        assert not fresh.assisted(c, CHILD, 'a', 'q', at)
    c.rollback()
    c.close()


def test_sqlite_change_guards_are_not_claimed_to_detect_a_read_only_transaction_restart():
    # Deliberate private-contract misuse, NOT the production consumer path.
    # Keep this limitation executable rather than claiming PRAGMAs detect epochs.
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.execute('CREATE TABLE rubric_assessment(assessment_id,child_id,step_id,payload_json)')
    at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    c.execute('INSERT INTO rubric_assessment VALUES(?,?,?,?)', ('a', CHILD, 's',
        json.dumps({'question': 'Original question', 'responded_at': at.isoformat()})))
    c.commit()
    c.execute('BEGIN')
    with RubricPriorIndex(c, CHILD) as index:
        assert not index.assisted(c, CHILD, 'a', 'q', at)
        changes, version = c.total_changes, c.execute('PRAGMA data_version').fetchone()[0]
        c.commit()
        c.execute('BEGIN')
        assert c.total_changes == changes and c.execute('PRAGMA data_version').fetchone()[0] == version
        assert not index.assisted(c, CHILD, 'a', 'q', at)
    # The actual consumer exits its with block before returning to the caller.
    with pytest.raises(RuntimeError, match='SCOPE_CHANGED'):
        index.assisted(c, CHILD, 'a', 'q', at)
    c.rollback()
    c.close()


def test_index_does_not_swallow_actual_sql_fault(gate):
    current, _, _ = gate.run(local=True)
    with gate.db.connect() as c:
        c.execute('BEGIN')
        with RubricPriorIndex(c, CHILD) as index:
            c.set_authorizer(lambda op, table, *_: sqlite3.SQLITE_DENY
                if op == sqlite3.SQLITE_READ and table == 'rubric_assessment' else sqlite3.SQLITE_OK)
            with pytest.raises(sqlite3.DatabaseError):
                index.assisted(c, CHILD, current['evaluation']['rubric_assessment']['assessment_id'], 'q', gate.clock[0])


@pytest.mark.parametrize('score', [0, 3])
def test_current_written_review_withdrawal_and_restart_never_reuse_cached_authority(writing_chain, score):
    from home_study_agent.database import Database
    db, sid, prompts, originals, _, near, _ = writing_chain
    text = 'Dear Ben, our concert was in the school hall. I liked the violin music because it was cheerful. Come with me next time. Best wishes, Mia.'
    assessment, attempt, body = reviewed_answer(db, sid, prompts, originals, near['diagnostic_item_id'], text, 'batch-retract')
    if score == 0:
        low = dict(body, expected_revision=1, client_action_id='batch-low-review',
            reason='Synthetic low score is not mastery',
            dimensions=[dict(d, score=0) for d in body['dimensions']])
        RubricEvidenceService(db).review(CHILD, assessment['assessment_id'], **low)
    now = datetime.now(timezone.utc)
    receipt = paired(db, assessment['step_id'], now)
    assert receipt and receipt['independent'] and receipt['correctness'] == score/3
    assert paired(Database(db.path), assessment['step_id'], now) == receipt
    RubricEvidenceService(db).review(CHILD, assessment['assessment_id'], expected_revision=2 if score == 0 else 1,
        client_action_id='batch-withdraw', reviewer_id='parent', reviewer_role='parent', operation='retract', reason='Synthetic withdrawal')
    assert paired(Database(db.path), assessment['step_id'], now) is None
    with db.connect() as c:
        assert c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE evidence_id=?',
            (attempt['attempt']['evidence_id'],)).fetchone()[0] == text


def test_same_connection_next_transaction_and_onboarding_child_switch_recheck_current_review(writing_chain):
    db, sid, prompts, originals, _, near, _ = writing_chain
    text = 'Dear Ben, our concert was in the school hall. I liked the violin music because it was cheerful. Come with me next time. Best wishes, Mia.'
    assessment, _, _ = reviewed_answer(db, sid, prompts, originals, near['diagnostic_item_id'], text, 'batch-next-tx')
    service = RubricEvidenceService(db)
    now = datetime.now(timezone.utc)
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        with RubricPriorIndex(c, CHILD) as original:
            first = service.reviewed_receipt(c, CHILD, assessment['step_id'], now=now, _prior_index=original)
            assert first and first['independent']
        with RubricPriorIndex(c, 'child-junior-3') as other:
            assert service.reviewed_receipt(c, 'child-junior-3', assessment['step_id'], now=now, _prior_index=other) is None
        with RubricPriorIndex(c, CHILD) as switched_back:
            assert service.reviewed_receipt(c, CHILD, assessment['step_id'], now=now, _prior_index=switched_back) == first
        c.commit()
        service.review(CHILD, assessment['assessment_id'], expected_revision=1,
            client_action_id='batch-next-tx-retract', reviewer_id='parent', reviewer_role='parent',
            operation='retract', reason='Synthetic current review withdrawal')
        c.execute('BEGIN')
        with pytest.raises(RuntimeError, match='SCOPE_CHANGED'):
            original.assisted(c, CHILD, assessment['assessment_id'], 'q', now)
        with RubricPriorIndex(c, CHILD) as fresh:
            assert service.reviewed_receipt(c, CHILD, assessment['step_id'], now=now, _prior_index=fresh) is None
        assert service.reviewed_receipt(c, CHILD, assessment['step_id'], now=now) is None
        assert c.total_changes == 0
        c.rollback()


def test_actual_audio_original_typed_hint_and_source_retraction_differential(tmp_path, monkeypatch):
    from test_typed_tutor_exposure_http import reviewed_material, saved_hint, update, facts
    with reviewed_material(tmp_path, monkeypatch) as case:
        now = datetime.now(timezone.utc)
        step = case.assessment['step_id']
        before = facts(case.db)
        first = paired(case.db, step, now)
        assert first and first['independent']
        turn, _ = saved_hint(case)
        hinted = paired(case.db, step, now)
        assert hinted and not hinted['independent'] and hinted['correctness'] == first['correctness']
        answer_at = datetime.fromisoformat(case.answer['attempt']['created_at'])
        update(case.db, 'UPDATE tutor_turn SET created_at=? WHERE turn_id=?',
            ((answer_at+timedelta(microseconds=1)).isoformat(), turn['turn_id']))
        assert paired(case.db, step, now)['independent']
        update(case.db, "UPDATE diagnostic_item SET stimulus_text=stimulus_text||' revised' WHERE diagnostic_item_id=?",
            (case.prompt['diagnostic_item_id'],))
        assert paired(case.db, step, now) is None
        assert facts(case.db) == before
