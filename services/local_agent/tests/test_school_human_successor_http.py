"""Real parent decisions; synthetic authored originals, no model calibration."""
import pytest

from test_school_open_reviews import CHILD, school_case, answer_and_review


def rows(db, evidence_id):
    with db.connect() as c:
        return [dict(r) for r in c.execute(
            'SELECT * FROM review_task WHERE child_id=? AND source_evidence_id=? ORDER BY rowid',
            (CHILD, evidence_id))]


@pytest.mark.parametrize('unknown_legacy', [False, True], ids=['explicit-human-successor', 'unknown-legacy-stays-cancelled'])
def test_parent_reapproval_preserves_root_and_creates_exactly_one_successor(school_case, unknown_legacy):
    db, client, headers, sid, baseline, near = school_case[:6]
    _, scored, assessment, path, approve = answer_and_review(school_case, key='human-root')
    evidence_id = scored['attempt']['evidence_id']
    original = rows(db, evidence_id)
    assert len(original) == 1
    root = original[0]
    assert root['status'] == 'pending'
    assert root['score_status'] == 'reviewed_human'
    assert root['score_authority_id']
    assert root['predecessor_review_task_id'] is None
    assert root['diagnostic_item_id'] == near['diagnostic_item_id']
    gateway_calls = len(client.school_gateway.requests)

    if unknown_legacy:
        # Negative fixture only: an old manual cancellation with no producer proof.
        # No successful path fabricates a review row or a parent decision.
        with db.connect() as c:
            c.execute("UPDATE review_task SET status='cancelled',score_authority_id=NULL,score_status=NULL WHERE review_task_id=?",
                      (root['review_task_id'],))
            c.commit()
    else:
        retract = client.post(path, headers=headers, json={
            'operation': 'retract', 'expected_revision': 1,
            'client_action_id': 'human-root-withdraw', 'reason': 'Rechecking the original response evidence'})
        assert retract.status_code == 200, retract.text
        assert rows(db, evidence_id)[0]['status'] == 'cancelled'

    cancelled = rows(db, evidence_id)[0]
    # Restart/read cannot itself recreate cancelled work.
    client = client.school_reopen()
    unlocked = client.post('/v1/parent/unlock', json={'pin': '8264'})
    assert unlocked.status_code == 200, unlocked.text
    headers = {'X-HSC-Parent': unlocked.json()['parent_token']}
    assert client.get(path.removesuffix('/review'), headers=headers).status_code == 200
    assert rows(db, evidence_id) == [cancelled]

    revised = {**approve, 'expected_revision': 1 if unknown_legacy else 2,
               'client_action_id': 'human-root-explicit-new-approval'}
    response = client.post(path, headers=headers, json=revised)
    assert response.status_code == 200, response.text
    event = response.json()['event']
    current = rows(db, evidence_id)
    assert current[0] == cancelled, 'The original cancelled obligation must remain immutable'
    if unknown_legacy:
        assert current == [cancelled], 'Unknown legacy cancellation is not permission to recreate work'
    else:
        assert len(current) == 2, 'Explicit new human approval has no successor to the cancelled root'
        successor = current[1]
        assert successor['status'] == 'pending'
        assert successor['predecessor_review_task_id'] == root['review_task_id']
        assert successor['review_task_id'] != root['review_task_id']
        assert successor['score_authority_id'] != root['score_authority_id']
        assert successor['score_status'] == 'reviewed_human'
        for field in ('child_id', 'source_evidence_id', 'knowledge_node_id', 'diagnostic_item_id', 'interval_label'):
            assert successor[field] == root[field]
        assert sum(r['status'] == 'pending' for r in current) == 1

    for restart in (False, True):
        if restart:
            client = client.school_reopen()
            headers = {'X-HSC-Parent': client.post('/v1/parent/unlock', json={'pin': '8264'}).json()['parent_token']}
        replay = client.post(path, headers=headers, json=revised)
        assert replay.status_code == 200, replay.text
        assert replay.json()['event'] == event
        assert rows(db, evidence_id) == current
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id=?', (CHILD,)).fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment WHERE child_id=?', (CHILD,)).fetchone()[0] == 1
    assert len(client.school_gateway.requests) == gateway_calls
