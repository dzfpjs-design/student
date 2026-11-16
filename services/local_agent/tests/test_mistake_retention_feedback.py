"""Retention feedback must share the current mistake mastery evidence gate."""
from datetime import timedelta

import pytest

from home_study_agent.learning_feedback import build_learning_feedback
from test_mistake_gate_reliability import flow, prepare, submit, CHILD, SECOND


@pytest.mark.parametrize('change', ['unchanged', 'early', 'baseline_hint', 'baseline_missing',
                                  'late_hint', 'wrong', 'source', 'new_gate', 'other_child'])
def test_retention_is_projected_from_valid_prerequisites_readonly(flow, change):
    db, _, _, clock = flow
    book, mid = prepare(flow)
    submit(book, mid)
    with db.connect() as c:
        baseline = c.execute("SELECT evidence_id,created_at FROM mistake_attempt WHERE mistake_id=? AND stage='near_transfer'", (mid,)).fetchone()
    clock[0] += timedelta(hours=49)
    submit(book, mid, 'delayed_48h')
    with db.connect() as c:
        delayed = c.execute("SELECT evidence_id FROM mistake_attempt WHERE mistake_id=? AND stage='delayed_48h'", (mid,)).fetchone()[0]
        if change == 'early':
            early = (clock[0] - timedelta(hours=2)).isoformat()
            c.execute('UPDATE mistake_attempt SET created_at=? WHERE evidence_id=?', (early, delayed))
            c.execute('UPDATE learning_evidence SET occurred_at=? WHERE evidence_id=?', (early, delayed))
        elif change in {'baseline_hint', 'late_hint'}:
            eid = baseline['evidence_id'] if change == 'baseline_hint' else delayed
            for table in ('mistake_attempt', 'learning_evidence'):
                c.execute('UPDATE ' + table + ' SET hint_level=1,independent=0 WHERE evidence_id=?', (eid,))
        elif change == 'baseline_missing':
            c.execute('DELETE FROM mistake_attempt WHERE evidence_id=?', (baseline['evidence_id'],))
        elif change == 'wrong':
            c.execute("UPDATE mistake_attempt SET submitted_answer='17',correctness=0 WHERE evidence_id=?", (delayed,))
            c.execute('UPDATE learning_evidence SET correctness=0 WHERE evidence_id=?', (delayed,))
        elif change == 'source':
            c.execute("UPDATE diagnostic_item SET prompt='changed question' WHERE diagnostic_item_id=?", (SECOND,))
        elif change == 'new_gate':
            c.execute('UPDATE mistake_mastery_gate SET updated_at=? WHERE mistake_id=?', (clock[0].isoformat(), mid))
        elif change == 'other_child':
            c.execute("UPDATE mistake_attempt SET child_id='child-junior-3' WHERE evidence_id=?", (delayed,))
        c.commit()
        c.execute('PRAGMA query_only=ON')
        writes = c.total_changes
        feedback = build_learning_feedback(c, CHILD, now=clock[0])
        assert c.total_changes == writes
        nodes = feedback['nodes']
        assert sum(n['retention_success_count'] for n in nodes) == (change == 'unchanged'), feedback
        if change == 'unchanged':
            assert nodes[0]['status'] == 'retention_observed'
            assert nodes[0]['retention_basis_refs'] == [baseline['evidence_id']]
            assert not nodes[0]['mastery_claim_allowed']
        assert build_learning_feedback(c, 'child-junior-3', now=clock[0])['nodes'] == []
