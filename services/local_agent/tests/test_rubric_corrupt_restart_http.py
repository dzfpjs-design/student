"""A damaged original score must not prevent unrelated learning from starting."""
import pytest

from test_rubric_qualification_queue_http import consumer_case, read, work, ROOT
import test_school_open_reviews as school
from test_written_score_corrupt_history import corrupt_history
from test_writing_calibrated_gate import authority


@pytest.mark.parametrize('bad', ['{', '{"source_evidence_id":"missing"}'])
def test_corrupt_rubric_restart_preserves_original_and_serves_other_child(consumer_case, monkeypatch, bad):
    db, client, headers = consumer_case[:3]
    evidence, target, _, _ = authority(monkeypatch, consumer_case, 3)
    with db.connect() as c:
        corrupt_history(c, target['assessment_id'], bad)
        c.commit()
        original = dict(c.execute('SELECT * FROM rubric_assessment WHERE assessment_id=?',
                                 (target['assessment_id'],)).fetchone())
        calls = c.execute('SELECT count(*) FROM model_call').fetchone()[0]
        outcomes = c.execute('SELECT count(*) FROM agent_outcome').fetchone()[0]
    client = client.school_reopen()
    assert client.get('/v1/health').status_code == 200
    headers = {'X-HSC-Parent': client.post('/v1/parent/unlock', json={'pin': '8264'}).json()['parent_token']}
    assert client.get(ROOT, headers=headers).status_code == 409
    page = work(client, headers)
    source = next(s for s in page['sources'] if s['kind'] == 'rubric')
    assert source['status'] == 'unavailable' and source['total'] is None
    other = read(client, '/v1/parent/children/child-primary-4/learning-rubrics', headers)
    assert other['items'] == []
    started = client.post('/v1/sessions', json={'child_id': 'child-primary-4',
        'client_action_id': 'healthy-child-after-corrupt-restart'})
    assert started.status_code == 409 and started.json()['code'] == 'ACTIVE_SESSION_EXISTS'
    switched = client.post(f'/v1/sessions/{consumer_case[3]}/switch-child', json={
        'target_child_id': 'child-primary-4', 'client_action_id': 'explicit-switch-after-corrupt-restart'})
    assert switched.status_code == 200, switched.text
    session = switched.json()
    assert session['child_id'] == 'child-primary-4'
    assert client.get('/v1/sessions/' + session['session_id']).status_code == 200
    with db.connect() as c:
        assert dict(c.execute('SELECT * FROM rubric_assessment WHERE assessment_id=?',
                             (target['assessment_id'],)).fetchone()) == original
        assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE evidence_id=?', (evidence,)).fetchone()[0] is None
        assert c.execute('SELECT count(*) FROM model_call').fetchone()[0] == calls
        assert c.execute('SELECT count(*) FROM agent_outcome').fetchone()[0] == outcomes
