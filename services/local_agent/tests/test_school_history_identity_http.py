"""Real parent reviews with unrelated historical prompts; no invented scores."""
import json
import pytest

from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from test_school_open_reviews import school_case, answer_and_review, next_day, CHILD


def as_legacy(db, prompt):
    """Import the actual pre-column row shape, preserving its original full seal."""
    with db.connect() as c:
        row = dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone())
        row.pop('identity_snapshot_json', None)
        c.execute('DELETE FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],))
        c.execute('INSERT INTO diagnostic_prompt (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
        c.commit()


def test_unknown_legacy_history_preserves_pending_and_recovers_exact_obligation(school_case):
    from home_study_agent.school_open_reviews import reconcile_school_reviews
    db = school_case[0]
    old = history(school_case)
    as_legacy(db, old)
    _, scored, _, _, _ = answer_and_review(school_case)
    with db.connect() as c:
        before = dict(c.execute('SELECT * FROM review_task WHERE source_evidence_id=?', (scored['attempt']['evidence_id'],)).fetchone())
        original = c.execute('SELECT explanation FROM diagnostic_item WHERE diagnostic_item_id=?', (old['diagnostic_item_id'],)).fetchone()[0]
        c.execute("UPDATE diagnostic_item SET explanation=explanation||' changed' WHERE diagnostic_item_id=?", (old['diagnostic_item_id'],))
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        result = reconcile_school_reviews(c, CHILD)
        c.commit()
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?', (before['review_task_id'],)).fetchone()[0] == 'pending'
        assert any(r.get('error_code') == 'SCHOOL_REVIEW_HISTORY_UNAVAILABLE' and r.get('retryable') for r in result['blocked']), result
        assert c.execute('SELECT identity_snapshot_json FROM diagnostic_prompt WHERE prompt_id=?', (old['prompt_id'],)).fetchone()[0] is None
        c.execute('UPDATE diagnostic_item SET explanation=? WHERE diagnostic_item_id=?', (original, old['diagnostic_item_id']))
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        recovered = reconcile_school_reviews(c, CHILD)
        c.commit()
        assert not recovered['blocked'] and not recovered['created'], recovered
        assert c.execute('SELECT COUNT(*) FROM review_task WHERE source_evidence_id=?', (scored['attempt']['evidence_id'],)).fetchone()[0] == 1


@pytest.mark.parametrize('change', ['grade', 'node_retired'])
def test_legacy_unchanged_full_seal_survives_current_eligibility_change(school_case, change):
    db = school_case[0]
    old = history(school_case, previous_grade=change == 'grade')
    as_legacy(db, old)
    if change == 'node_retired':
        with db.connect() as c:
            c.execute("UPDATE knowledge_node SET status='retired' WHERE knowledge_node_id=?", (old['item']['knowledge_node_id'],))
            c.commit()
    _, scored, _, _, _ = answer_and_review(school_case)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM review_task WHERE source_evidence_id=? AND status='pending'", (scored['attempt']['evidence_id'],)).fetchone()[0] == 1


def history(case, *, previous_grade=False):
    db, _, _, sid = case[:4]
    item_id = 'diag-j3-chinese-reading-1'
    if previous_grade:
        with db.connect() as c:
            c.execute("UPDATE child_profile SET grade='初二' WHERE child_id=?", (CHILD,))
            c.execute("UPDATE knowledge_node SET grade_band='初二' WHERE knowledge_node_id=(SELECT knowledge_node_id FROM diagnostic_item WHERE diagnostic_item_id=?)", (item_id,))
            c.commit()
    service = DiagnosticPromptService(db)
    prompt = service.prepare(sid, track='school', preferred_item_id=item_id,
        client_action_id='unrelated-history')['prompt']
    service.cancel(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'])
    if previous_grade:
        with db.connect() as c:
            c.execute("UPDATE child_profile SET grade='初三' WHERE child_id=?", (CHILD,))
            c.commit()
    return prompt


@pytest.mark.parametrize('change', ['node_retired', 'item_retired', 'explanation', 'grade'])
def test_real_parent_review_keeps_unrelated_history_without_current_eligibility(school_case, change):
    db = school_case[0]
    old = history(school_case, previous_grade=change == 'grade')
    with db.connect() as c:
        if change == 'node_retired':
            c.execute("UPDATE knowledge_node SET status='retired' WHERE knowledge_node_id=?", (old['item']['knowledge_node_id'],))
        elif change == 'item_retired':
            c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (old['diagnostic_item_id'],))
        elif change == 'explanation':
            c.execute("UPDATE diagnostic_item SET explanation=explanation||' revised explanation' WHERE diagnostic_item_id=?", (old['diagnostic_item_id'],))
        c.commit()
    _, scored, _, _, _ = answer_and_review(school_case)
    with db.connect() as c:
        rows = c.execute('SELECT * FROM review_task WHERE source_evidence_id=?', (scored['attempt']['evidence_id'],)).fetchall()
        assert len(rows) == 1 and rows[0]['status'] == 'pending', 'Unrelated history blocked the actual parent-approved review'
        assert rows[0]['diagnostic_item_id'] == school_case[5]['diagnostic_item_id']


def test_new_frozen_history_survives_real_voice_start_and_restart(school_case, monkeypatch):
    old = history(school_case)
    with school_case[0].connect() as c:
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (old['diagnostic_item_id'],))
        c.commit()
    answer_and_review(school_case)
    case, task, prompt, _, _ = next_day(school_case, monkeypatch, key='frozen-history')
    client = case[1].school_reopen()
    got = client.get(f'/v1/sessions/{case[3]}/diagnostic-prompt', params={'prompt_id': prompt['prompt_id']})
    assert got.status_code == 200 and got.json()['status'] == 'waiting_answer', got.text
    with case[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE review_task_id=?', (task['source_ref'],)).fetchone()[0] == 0
