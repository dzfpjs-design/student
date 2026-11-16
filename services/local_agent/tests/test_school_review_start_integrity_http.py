"""Real TCP review recovery; synthetic sources/score admission, no calibration run."""
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from home_study_agent import school_review_execution
from home_study_agent.school_open_reviews import SchoolReviewError, reconcile_school_reviews
import test_school_open_reviews as school
from test_school_review_resume_origin_http import ResumeGateway
from test_voice_runtime_binding import capture, interpret
from test_writing_calibrated_gate import authority


@contextmanager
def _http_client(application, **kwargs):
    with closing(socket.socket()) as listener:
        listener.bind(('127.0.0.1', 0))
        server = uvicorn.Server(uvicorn.Config(application, log_level='error', access_log=False))
        failures = []

        def run():
            try:
                server.run(sockets=[listener])
            except BaseException as error:
                failures.append(error)

        worker = threading.Thread(target=run, name='school-start-integrity-http', daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 30
            while worker.is_alive() and not server.started and time.monotonic() < deadline:
                time.sleep(.02)
            assert server.started and not failures, failures
            with httpx.Client(base_url=f'http://127.0.0.1:{listener.getsockname()[1]}',
                              timeout=30, trust_env=False) as client:
                client.app = application
                yield client
        finally:
            server.should_exit = True
            worker.join(10)
            if worker.is_alive():
                server.force_exit = True
                worker.join(5)
            assert not worker.is_alive(), 'owned school HTTP server did not drain'
            assert not failures, failures


@pytest.fixture
def school_http_case(tmp_path, monkeypatch):
    monkeypatch.setattr(school, 'TestClient', _http_client)
    yield from school.school_case.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param='public'))


def _observe_guard(monkeypatch):
    original = school_review_execution.validate_start
    calls = []

    def checked(c, item, turn_id):
        prompt = c.execute('SELECT status FROM diagnostic_prompt WHERE review_task_id=?',
                           (item['source_ref'],)).fetchone()
        call = {'prompt_status': prompt[0] if prompt else None, 'error': None}
        calls.append(call)
        try:
            return original(c, item, turn_id)
        except Exception as error:
            call['error'] = str(error)
            raise

    monkeypatch.setattr(school_review_execution, 'validate_start', checked)
    return calls


def _unlock(client):
    response = client.post('/v1/parent/unlock', json={'pin': '8264'})
    assert response.status_code == 200, response.text
    return {'X-HSC-Parent': response.json()['parent_token']}


def _answered_review(case, monkeypatch, *, calibrated_successor=False):
    db, client, headers = case[:3]
    root = None
    if calibrated_successor:
        # The admission boundary is explicitly synthetic; journals, parent review,
        # successor creation and later recovery all use real production services.
        evidence, _, _, (path, body) = authority(monkeypatch, case, 3)
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            root_id = reconcile_school_reviews(c, school.CHILD)['created'][0]
            root = dict(c.execute('SELECT * FROM review_task WHERE review_task_id=?', (root_id,)).fetchone())
            assert root['score_status'] == 'calibrated_model'
            c.commit()
        reviewed = client.post(path, headers=headers, json=body)
        assert reviewed.status_code == 200, reviewed.text
        with db.connect() as c:
            rows = [dict(r) for r in c.execute('SELECT * FROM review_task WHERE source_evidence_id=?', (evidence,))]
            cancelled = next(r for r in rows if r['review_task_id'] == root_id)
            pending = [r for r in rows if r['status'] == 'pending']
            assert cancelled['status'] == 'cancelled' and len(pending) == 1
            assert pending[0]['predecessor_review_task_id'] == root_id
            assert pending[0]['score_status'] == 'reviewed_human'
            root = cancelled
        # No synthetic score admission may affect the subsequent review answer.
        from home_study_agent.learning_rubric import RubricEvidenceService
        monkeypatch.setattr(RubricEvidenceService, 'calibrated_receipt', lambda *a, **k: None)
    else:
        school.answer_and_review(case, score=0)
    case, slot, prompt, _, _ = school.next_day(case, monkeypatch, key='integrity-review')
    _, answer, _, path, body = school.answer_and_review(case, prompt=prompt, key='integrity-answer', score=3, submit=False)
    client = case[1]
    monkeypatch.setattr(client.school_gateway, 'complete', ResumeGateway().complete)
    reopened = client.school_reopen()
    case = (case[0], reopened, _unlock(reopened), *case[3:])
    with db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (slot['plan_item_id'],)).fetchone()[0] == 'paused'
        assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()[0] == 'answered'
    return case, slot, prompt, answer, path, body, root


def _resume_command(case, monkeypatch, key):
    _, client, _, sid, *_, settings = case
    text = '请继续原来的复习任务。'
    voice = capture(client, settings, monkeypatch, sid, text, key=key+'-voice', child=school.CHILD)
    turn = interpret(client, sid, text, key, voice)
    assert turn['runtime_capability'] == 'task.resume', turn
    return f"/v1/agent-turns/{turn['agent_turn_id']}/execute"


def _state(db, slot, prompt):
    with db.connect() as c:
        return {
            'plan': dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (slot['plan_item_id'],)).fetchone()),
            'events': [dict(r) for r in c.execute('SELECT * FROM plan_item_event WHERE plan_item_id=? ORDER BY rowid', (slot['plan_item_id'],))],
            'prompt': dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()),
            'attempts': [dict(r) for r in c.execute('SELECT * FROM diagnostic_attempt ORDER BY rowid')],
            'reviews': [dict(r) for r in c.execute('SELECT * FROM review_task ORDER BY rowid')],
        }


@pytest.mark.parametrize('calibrated_successor', [False, True], ids=['human-root', 'calibrated-to-human-successor'])
def test_full_start_guard_accepts_saved_answer_new_score_and_successor(school_http_case, monkeypatch, calibrated_successor):
    case, slot, prompt, answer, path, body, root = _answered_review(
        school_http_case, monkeypatch, calibrated_successor=calibrated_successor)
    db, client, headers, sid = case[:4]
    before = _state(db, slot, prompt)
    calls = _observe_guard(monkeypatch)
    resume = _resume_command(case, monkeypatch, 'valid-resume')
    response = client.post(resume)
    assert response.status_code == 200, response.text
    assert {'prompt_status': 'answered', 'error': None} in calls
    result = response.json()['execution']['result']
    assert result['status'] == 'active'
    assert result['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']
    assert result['diagnostic_prompt']['attempt']['attempt_id'] == answer['attempt']['attempt_id']
    active = _state(db, slot, prompt)
    replay = client.post(resume)
    assert replay.status_code == 200, replay.text
    replayed = replay.json()['execution']['result']
    for key in ('plan_item_id', 'status', 'started_at'):
        assert replayed[key] == result[key]
    # A control replay is a receipt, not a fresh question delivery.
    current_prompt = client.get(f'/v1/sessions/{sid}/diagnostic-prompt', params={'prompt_id': prompt['prompt_id']})
    assert current_prompt.status_code == 200, current_prompt.text
    assert current_prompt.json()['prompt']['prompt_id'] == prompt['prompt_id']
    assert current_prompt.json()['attempt']['attempt_id'] == answer['attempt']['attempt_id']
    assert _state(db, slot, prompt) == active
    assert _state(db, slot, prompt)['attempts'] == before['attempts']
    reviewed = client.post(path, headers=headers, json=body)
    assert reviewed.status_code == 200, reviewed.text
    # Resume the now-reviewed original too, before completing the scheduled task.
    client = client.school_reopen()
    case = (db, client, _unlock(client), *case[3:])
    calls.clear()
    resumed_reviewed = client.post(_resume_command(case, monkeypatch, 'reviewed-resume'))
    assert resumed_reviewed.status_code == 200, resumed_reviewed.text
    assert {'prompt_status': 'answered', 'error': None} in calls
    resumed_prompt = resumed_reviewed.json()['execution']['result']['diagnostic_prompt']
    assert resumed_prompt['prompt']['prompt_id'] == prompt['prompt_id']
    assert resumed_prompt['attempt']['attempt_id'] == answer['attempt']['attempt_id']
    complete = f"/v1/plan-items/{slot['plan_item_id']}/complete"
    completion = {'actual_minutes': 1, 'client_action_id': 'integrity-complete'}
    done = client.post(complete, json=completion)
    assert done.status_code == 200, done.text
    after = _state(db, slot, prompt)
    assert after['plan']['status'] == 'completed'
    assert after['attempts'] == before['attempts']
    assert after['plan']['started_at'] == before['plan']['started_at']
    assert sum(e['event_type'] == 'started' for e in after['events']) == 1
    assert sum(e['event_type'] == 'resumed' for e in after['events']) == 2
    current = next(r for r in after['reviews'] if r['review_task_id'] == slot['source_ref'])
    assert current['status'] == 'completed' and current['result_evidence_id'] == answer['attempt']['evidence_id']
    far = [r for r in after['reviews'] if r['source_evidence_id'] == answer['attempt']['evidence_id'] and r['status'] == 'pending']
    assert len(far) == 1 and far[0]['interval_label'] == 'school_open_far'
    assert far[0]['diagnostic_item_id'] == case[6]['diagnostic_item_id']
    if root:
        assert next(r for r in after['reviews'] if r['review_task_id'] == root['review_task_id']) == root
    reopened = client.school_reopen()
    read = reopened.get(f"/v1/sessions/{sid}/diagnostic-prompt", params={'prompt_id': prompt['prompt_id']})
    assert read.status_code == 200 and read.json()['attempt']['attempt_id'] == answer['attempt']['attempt_id']
    assert reopened.post(complete, json=completion).status_code == 200
    assert _state(db, slot, prompt) == after


@pytest.mark.parametrize('fault', ['started_missing', 'started_time', 'other_child_attempt', 'future_attempt'])
def test_full_start_guard_rejects_corrupt_answered_review(school_http_case, monkeypatch, fault):
    case, slot, prompt, answer, _, _, _ = _answered_review(school_http_case, monkeypatch)
    db, client = case[:2]
    resume = _resume_command(case, monkeypatch, 'corrupt-resume')
    # Corrupt only after real voice admission. A rejection must reach the actual
    # answered-prompt start guard, not merely fail to find a resume command.
    with db.connect() as c:
        if fault == 'started_missing':
            c.execute("DELETE FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (slot['plan_item_id'],))
        elif fault == 'started_time':
            c.execute("UPDATE plan_item_event SET occurred_at='2000-01-01T00:00:00+00:00' WHERE plan_item_id=? AND event_type='started'", (slot['plan_item_id'],))
        elif fault == 'other_child_attempt':
            c.execute("UPDATE diagnostic_attempt SET child_id='child-primary-4' WHERE attempt_id=?", (answer['attempt']['attempt_id'],))
        else:
            c.execute('UPDATE diagnostic_attempt SET created_at=? WHERE attempt_id=?',
                      ((datetime.fromisoformat(answer['attempt']['created_at']) + timedelta(days=1)).isoformat(), answer['attempt']['attempt_id']))
        c.commit()
    before = _state(db, slot, prompt)
    if fault == 'other_child_attempt':
        # HTTP rejects this at the earlier prompt-read ownership boundary too.
        # Independently prove validate_start keeps its own attempt-owner check.
        with db.connect() as c:
            c.execute('BEGIN')
            item = c.execute('''SELECT pi.*,dp.child_id,dp.session_id FROM plan_item pi
                JOIN daily_plan dp USING(plan_id) WHERE pi.plan_item_id=?''', (slot['plan_item_id'],)).fetchone()
            origin = c.execute('''SELECT p.agent_turn_id FROM agent_plan p JOIN agent_step s USING(plan_id)
                WHERE s.capability_name='task.start'
                AND json_extract(p.context_manifest_json,'$.goal_prompt_binding.prompt_id')=?''', (prompt['prompt_id'],)).fetchone()
            assert origin
            with pytest.raises(SchoolReviewError, match='SCHOOL_REVIEW_ANSWER_BEFORE_START'):
                school_review_execution.validate_start(c, item, origin[0])
    calls = _observe_guard(monkeypatch)
    rejected = client.post(resume)
    assert rejected.status_code in {409, 422}, rejected.text
    if fault == 'other_child_attempt':
        assert rejected.json()['code'] == 'ANSWER_TASK_RESUME_PROMPT_CHANGED'
    else:
        assert {'prompt_status': 'answered', 'error': 'SCHOOL_REVIEW_ANSWER_BEFORE_START'} in calls, (calls, rejected.text)
    assert _state(db, slot, prompt) == before
    # Failed execution replays must not append resumed events or alter answers.
    retry = client.post(resume)
    assert retry.status_code in {409, 422}, retry.text
    assert _state(db, slot, prompt) == before
