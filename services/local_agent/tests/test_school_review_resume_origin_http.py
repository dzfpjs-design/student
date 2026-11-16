"""Real saved school original, system pause, new captured resume and exact origin."""
import json

import pytest

from home_study_agent.model_gateway import ModelResult
from test_school_open_reviews import school_case, answer_and_review, next_day, CHILD
from test_voice_runtime_binding import capture, interpret


class ResumeGateway:
    configured = True

    async def complete(self, request):
        assert request.request_type == 'voice_interaction', request.request_type
        return ModelResult(content=json.dumps({'role':'control','control':'task.resume',
            'quote':'请继续原来的复习任务。','confidence':.99,
            'selector':{'subject':None,'task_kind':None,'relative_order':None}}),
            provider='synthetic',model='synthetic',latency_ms=1)


def paused_case(case, monkeypatch):
    answer_and_review(case, score=0)
    case, slot, prompt, _, _ = next_day(case, monkeypatch, key='origin-recovery')
    _, scored, _, _, _ = answer_and_review(case, prompt=prompt, key='saved-original', submit=False)
    db, client, headers, sid, *rest = case
    monkeypatch.setattr(client.school_gateway, 'complete', ResumeGateway().complete)
    reopened = client.school_reopen()
    return (db, reopened, headers, sid, *rest), slot, prompt, scored


def resume(case, monkeypatch, key):
    _, client, _, sid, _, _, _, _, settings = case
    text = '请继续原来的复习任务。'
    voice = capture(client, settings, monkeypatch, sid, text, key=key+'-voice', child=CHILD)
    turn = interpret(client, sid, text, key, voice)
    assert turn['runtime_capability'] == 'task.resume', turn
    return turn, client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")


def test_answered_school_original_resumes_from_exact_started_turn(school_case, monkeypatch):
    case, slot, prompt, scored = paused_case(school_case, monkeypatch)
    db, client, _, sid = case[:4]
    with db.connect() as c:
        original = dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',
                                 (scored['attempt']['attempt_id'],)).fetchone())
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (slot['plan_item_id'],)).fetchone()[0] == 'paused'
    turn, response = resume(case, monkeypatch, 'resume-saved-original')
    assert response.status_code == 200, response.text
    result = response.json()['execution']['result']
    assert result['status'] == 'active'
    assert result['diagnostic_prompt']['status'] == 'answered'
    assert result['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']
    replay = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert replay.status_code == 200, replay.text
    reopened = client.school_reopen()
    next_case = (db, reopened, *case[2:])
    _, again = resume(next_case, monkeypatch, 'resume-saved-original-after-restart')
    assert again.status_code == 200, again.text
    assert again.json()['execution']['result']['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']
    with db.connect() as c:
        assert dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',
                              (original['attempt_id'],)).fetchone()) == original
        assert original['correctness'] is None
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE review_task_id=?', (prompt['review_task_id'],)).fetchone()[0] == 1
        events = [r[0] for r in c.execute('SELECT event_type FROM plan_item_event WHERE plan_item_id=?', (slot['plan_item_id'],))]
        assert events.count('started') == 1 and events.count('resumed') == 2
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?', (prompt['review_task_id'],)).fetchone()[0] == 'pending'


@pytest.mark.parametrize('fault', ['withdraw', 'cancel', 'missing_origin', 'other_child', 'missing_started', 'different_operation'])
def test_new_resume_cannot_borrow_invalid_original_scope(school_case, monkeypatch, fault):
    case, slot, prompt, scored = paused_case(school_case, monkeypatch)
    db, client, _, sid, _, _, _, supply, settings = case
    text = '请继续原来的复习任务。'
    voice = capture(client, settings, monkeypatch, sid, text, key='rejected-resume-voice', child=CHILD)
    turn = interpret(client, sid, text, 'rejected-resume', voice)
    assert turn['runtime_capability'] == 'task.resume', turn
    if fault == 'withdraw':
        headers = {'X-HSC-Parent':client.post('/v1/parent/unlock',json={'pin':'8264'}).json()['parent_token']}
        near = case[5]
        response = client.post(f"/api/parent/children/{CHILD}/curriculum-supply/{near['candidate_id']}/withdraw",
            headers=headers,json={'expected_version':4,'action_id':'withdraw-resume-source','note':'Synthetic source withdrawn before execution'})
        assert response.status_code == 200, response.text
    else:
        with db.connect() as c:
            if fault == 'cancel':
                c.execute("UPDATE plan_item SET status='cancelled' WHERE plan_item_id=?", (slot['plan_item_id'],))
            elif fault == 'missing_started':
                c.execute("DELETE FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (slot['plan_item_id'],))
            elif fault == 'different_operation':
                c.execute("UPDATE agent_step SET capability_name='task.pause' WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (turn['agent_turn_id'],))
            else:
                source = c.execute('''SELECT plan_id,context_manifest_json FROM agent_plan
                    WHERE json_extract(context_manifest_json,'$.goal_prompt_binding.prompt_id')=?''', (prompt['prompt_id'],)).fetchone()
                if fault == 'missing_origin':
                    manifest=json.loads(source['context_manifest_json'])
                    del manifest['goal_prompt_binding']
                    c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE plan_id=?',(json.dumps(manifest),source['plan_id']))
                else:
                    c.execute("UPDATE agent_plan SET child_id='child-primary-4' WHERE plan_id=?",(source['plan_id'],))
            c.commit()
    rejected = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert rejected.status_code in {409,422}, rejected.text
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='resumed'", (slot['plan_item_id'],)).fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE review_task_id=?',(prompt['review_task_id'],)).fetchone()[0] == 1
        assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE attempt_id=?',(scored['attempt']['attempt_id'],)).fetchone()[0] is None
