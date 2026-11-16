"""Foundation errors use the real voice, teaching, variant and evidence paths."""
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_goal_roadmap import flow, CHILD, ROOT
from test_goal_foundation_execution import foundation
from test_foundation_remediation import start, wrong
from test_tutor_child_model_controls import RecordingGateway
from test_voice_runtime_binding import capture, interpret


def say(client, sid, settings, monkeypatch, gateway, text, key):
    focus = client.get(f'/v1/sessions/{sid}/mistake-dialogue').json()['dialogue']
    reference = {'kind': 'mistake', 'binding': focus['answer_binding']} if focus.get('answer_binding') else None
    gateway.configured = False
    voice = capture(client, settings, monkeypatch, sid, text, reference, key=key+'-audio')
    turn = interpret(client, sid, text, key, voice)
    assert turn['action']['type'] == 'continue_mistake_dialogue', turn['action']
    gateway.configured = True
    result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    gateway.configured = False
    assert result.status_code == 200, result.text
    return result.json()


@pytest.fixture
def teaching_chain(foundation, monkeypatch):
    db, sid, settings = foundation
    gateway = RecordingGateway()
    gateway.configured = False
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        task, prompt = start(client, sid, settings, monkeypatch)
        result = wrong(client, sid, settings, monkeypatch, prompt)
        case = result['foundation_remediation']['mistake_id']
        confirmed = say(client, sid, settings, monkeypatch, gateway, '是的', 'chain-confirm')
        assert confirmed['execution']['result']['phase'] == 'diagnosis'
        diagnosed = say(client, sid, settings, monkeypatch, gateway, '我不知道为什么错了', 'chain-reason')
        assert diagnosed['execution']['result']['phase'] == 'diagnosis_probe'
        yield db, sid, settings, gateway, client, task, prompt, case, diagnosed['execution']['result']


@pytest.mark.parametrize('target_outcome', ['correct', 'wrong', 'withdrawn'])
def test_foundation_error_probe_teaching_and_independent_variant(teaching_chain, monkeypatch, target_outcome):
    db, sid, settings, gateway, client, task, original, case, probe = teaching_chain
    wrong_probe = say(client, sid, settings, monkeypatch, gateway, '答案是-999', 'chain-probe-answer')
    assert wrong_probe['execution']['result']['phase'] == 'remediation'
    assert wrong_probe['execution'].get('tutor_turn'), wrong_probe['execution']
    assert gateway.requests
    understood = say(client, sid, settings, monkeypatch, gateway, '我明白了', 'chain-understood')
    focus = understood['execution']['result']
    assert focus['phase'] == 'attempt', focus
    item = focus['state']['next_activity']['item']
    assert item['diagnostic_item_id'] not in {original['diagnostic_item_id'], probe['state']['next_activity']['item']['diagnostic_item_id']}
    with db.connect() as c:
        assert c.execute('SELECT status FROM mistake_case WHERE mistake_id=?', (case,)).fetchone()[0] != 'mastered'
        answer = json.loads(c.execute('SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?',
                            (item['diagnostic_item_id'],)).fetchone()[0])['exact_answers'][0]
    verified = say(client, sid, settings, monkeypatch, gateway, '答案是'+str(answer), 'chain-independent')
    assert verified['execution']['result']['phase'] == 'scheduled', verified['execution']
    with db.connect() as c:
        receipt = c.execute('SELECT * FROM mistake_attempt WHERE mistake_id=?', (case,)).fetchall()
        assert len(receipt) == 1
        assert receipt[0]['diagnostic_item_id'] == item['diagnostic_item_id']
        assert receipt[0]['correctness'] == 1
        assert c.execute('SELECT status FROM mistake_case WHERE mistake_id=?', (case,)).fetchone()[0] != 'mastered'
    due = verified['execution']['result']['state']['next_activity']['due_at']
    def post(path, body):
        response = client.post(path, json=body)
        assert response.status_code in {200, 201}, response.text
        return response.json()
    voice = capture(client, settings, monkeypatch, sid, '这项做完了', key='foundation-slot-end-audio')
    ended = interpret(client, sid, '这项做完了', 'foundation-slot-end', voice)
    finished = post(f"/v1/agent-turns/{ended['agent_turn_id']}/execute", {})
    assert finished['execution']['result']['status'] == 'completed'
    from test_writing_stage_review_consumers import advance_clock

    def next_day_plan(previous_sid, at, prefix):
        post(f'/v1/sessions/{previous_sid}/transition', {'expected_state': 'study', 'target_state': 'reflection',
            'reason': '今天先到这里', 'client_action_id': prefix+'-day-end'})
        post(f'/v1/sessions/{previous_sid}/reflection/complete', {'skipped': True, 'client_action_id': prefix+'-day-close'})
        advance_clock(monkeypatch, at)
        next_sid = post('/v1/sessions', {'child_id': CHILD, 'client_action_id': prefix+'-session'})['session_id']
        post(f'/v1/sessions/{next_sid}/transition', {'expected_state': 'check_in', 'target_state': 'classroom_recall',
            'reason': '新一天', 'client_action_id': prefix+'-recall'})
        post(f'/v1/sessions/{next_sid}/recalls/none', {'client_action_id': prefix+'-no-recall'})
        post(f'/v1/sessions/{next_sid}/transition', {'expected_state': 'classroom_recall', 'target_state': 'homework_capture',
            'reason': '作业核对', 'client_action_id': prefix+'-homework'})
        post(f'/v1/sessions/{next_sid}/homework/none', {'client_action_id': prefix+'-no-homework'})
        return next_sid, post(f'/v1/sessions/{next_sid}/plan/generate', {'client_action_id': prefix+'-plan'})

    sid2, workspace = next_day_plan(sid, datetime.fromisoformat(due) + timedelta(minutes=1), 'foundation-review-day')
    planned = next(p for p in workspace['plan_items'] if p['source_type'] == 'mistake_review' and p['source_ref'] == case)
    for item in workspace['plan_items']:
        if item['plan_item_id'] != planned['plan_item_id']:
            post(f"/v1/plan-items/{item['plan_item_id']}/reschedule", {'reason': 'Synthetic isolated review',
                'client_action_id': 'isolate-'+item['plan_item_id']})
    post(f'/v1/sessions/{sid2}/transition', {'expected_state': 'plan_review', 'target_state': 'study',
        'reason': '确认', 'client_action_id': 'foundation-review-study'})
    for phrase, key in [('开始任务', 'foundation-review-start'), ('打开错题本', 'foundation-review-open')]:
        voice = capture(client, settings, monkeypatch, sid2, phrase, key=key+'-audio')
        turn = interpret(client, sid2, phrase, key, voice)
        post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute", {})
    restored = client.get(f'/v1/sessions/{sid2}/mistake-dialogue').json()['dialogue']
    assert restored['mistake_id'] == case
    assert not restored['state'].get('foundation_origin')
    assert restored['state']['next_activity']['stage'] == 'delayed_48h'
    exercise = restored['state']['next_activity']['item']['diagnostic_item_id']
    with db.connect() as c:
        answer = json.loads(c.execute('SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?', (exercise,)).fetchone()[0])['exact_answers'][0]
    delayed = say(client, sid2, settings, monkeypatch, gateway, '答案是'+str(answer), 'foundation-delayed-answer')
    assert delayed['execution']['result']['plan_completion']['plan_item_id'] == planned['plan_item_id']
    from home_study_agent.learning_feedback import build_learning_feedback
    from home_study_agent.goal_roadmap import GoalRoadmapService
    from test_goal_roadmap import TARGET, OTHER
    with db.connect() as c:
        node = next(n for n in build_learning_feedback(c, CHILD)['nodes'] if n['knowledge_node_id'] == ROOT)
        assert node['retention_success_count'] >= 1
        assert not build_learning_feedback(c, OTHER)['nodes']
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (planned['plan_item_id'],)).fetchone()[0] == 'completed'
    route = GoalRoadmapService(db).read(CHILD, 'competition_math_path')
    assert route['next_daily_action']['knowledge_node_id'] == TARGET
    assert not route['mastery_claim_allowed']
    sid3, workspace = next_day_plan(sid2, datetime.fromisoformat(due) + timedelta(days=1), 'foundation-target-day')
    targets = [p for p in workspace['plan_items'] if p['source_type'] == 'competition_math']
    assert len(targets) == 1, [(p['source_type'], p['task_type']) for p in workspace['plan_items']]
    target = targets[0]
    assert target['task_type'] != 'goal_prerequisite'
    for item in workspace['plan_items']:
        if item['plan_item_id'] != target['plan_item_id']:
            post(f"/v1/plan-items/{item['plan_item_id']}/reschedule", {'reason': 'Synthetic isolated goal',
                'client_action_id': 'target-defer-'+item['plan_item_id']})
    post(f'/v1/sessions/{sid3}/transition', {'expected_state': 'plan_review', 'target_state': 'study',
        'reason': '确认计划', 'client_action_id': 'foundation-target-study'})
    if target_outcome == 'withdrawn':
        from home_study_agent.mistake_book import MistakeBookService
        MistakeBookService(db).confirm(case, CHILD, 'exclude', 'foundation-retention-withdrawal')
    voice = capture(client, settings, monkeypatch, sid3, '开始任务', key='foundation-target-start-audio')
    turn = interpret(client, sid3, '开始任务', 'foundation-target-start', voice)
    started = post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute", {})
    if target_outcome == 'withdrawn':
        assert started['execution']['result']['status'] == 'blocked'
        with db.connect() as c:
            assert c.execute('SELECT started_at FROM plan_item WHERE plan_item_id=?', (target['plan_item_id'],)).fetchone()[0] is None
            assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE session_id=?', (sid3,)).fetchone()[0] == 0
        return
    current = started['execution']['result']['diagnostic_prompt']['prompt']
    assert current['item']['knowledge_node_id'] == TARGET
    with db.connect() as c:
        expected = json.loads(c.execute('SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?',
            (current['diagnostic_item_id'],)).fetchone()[0])['exact_answers'][0]
    answer = '答案是'+(str(expected) if target_outcome == 'correct' else '-99999')
    voice = capture(client, settings, monkeypatch, sid3, answer, current, key='foundation-target-answer-audio')
    admitted = interpret(client, sid3, answer, 'foundation-target-answer', voice)
    assert admitted['action']['type'] == 'submit_diagnostic_answer'
    receipt = post(f'/v1/sessions/{sid3}/diagnostic-prompt/answer', {
        'prompt_id': current['prompt_id'], 'item_version': current['item_version'],
        'answer': admitted['action']['answer'], 'authorized_turn_id': admitted['agent_turn_id'],
        'client_action_id': 'foundation-target-score'})
    assert receipt['attempt']['correctness'] == (1 if target_outcome == 'correct' else 0)
    voice = capture(client, settings, monkeypatch, sid3, '这项做完了', key='foundation-target-done-audio')
    ended = interpret(client, sid3, '这项做完了', 'foundation-target-done', voice)
    finished = post(f"/v1/agent-turns/{ended['agent_turn_id']}/execute", {})
    assert finished['execution']['result']['status'] == 'completed'
    route = GoalRoadmapService(db).read(CHILD, 'competition_math_path')
    observed = next(n for n in route['baseline'] if n['knowledge_node_id'] == TARGET)
    assert observed['baseline_status'] == ('independent_observed' if target_outcome == 'correct' else 'recheck_needed')
    assert not route['mastery_claim_allowed']


@pytest.mark.parametrize('stop', ['paused', 'budget', 'withdrawn'])
def test_foundation_probe_cannot_bypass_original_task(teaching_chain, monkeypatch, stop):
    db, sid, settings, gateway, client, task, original, case, probe = teaching_chain
    with db.connect() as c:
        before = c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]
        if stop == 'paused':
            c.execute("UPDATE plan_item SET status='paused',active_segment_started_at=NULL WHERE plan_item_id=?", (task['plan_item_id'],))
        elif stop == 'budget':
            c.execute('UPDATE plan_item SET active_seconds=expected_minutes*60 WHERE plan_item_id=?', (task['plan_item_id'],))
        else:
            c.execute("UPDATE knowledge_node SET status='inactive' WHERE knowledge_node_id=?", (ROOT,))
        c.commit()
    reply = say(client, sid, settings, monkeypatch, gateway, '答案是-999', 'chain-stopped-answer')
    assert not reply['execution'].get('tutor_turn')
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == before
        assert c.execute('SELECT count(*) FROM tutor_turn').fetchone()[0] == 0
        event = c.execute('SELECT result_json FROM mistake_dialogue_event WHERE session_id=? ORDER BY rowid DESC LIMIT 1', (sid,)).fetchone()
        assert json.loads(event[0])['state']['foundation_hold']['utterance'] == '答案是-999'
    assert reply['execution']['result']['state'].get('foundation_hold')
    if stop == 'budget':
        with db.connect() as c:
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (task['plan_item_id'],)).fetchone()[0] == 'paused'


def test_pause_between_answer_reservation_and_scoring_preserves_unscored_words(teaching_chain, monkeypatch):
    from home_study_agent.mistake_dialogue import MistakeDialogueService
    db, sid, settings, gateway, client, task, original, case, probe = teaching_chain
    voice = capture(client, settings, monkeypatch, sid, '答案是-999',
        {'kind': 'mistake', 'binding': probe['answer_binding']}, key='late-pause-audio')
    turn = interpret(client, sid, '答案是-999', 'late-pause-answer', voice)
    checkpoint = MistakeDialogueService._checkpoint
    def pause_after_checkpoint(self, focus, operation):
        checkpoint(self, focus, operation)
        with db.connect() as c:
            c.execute("UPDATE plan_item SET status='paused',active_segment_started_at=NULL WHERE plan_item_id=?", (task['plan_item_id'],))
            c.commit()
    monkeypatch.setattr(MistakeDialogueService, '_checkpoint', pause_after_checkpoint)
    with db.connect() as c:
        count = c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]
    response = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert response.status_code == 409, response.text
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == count
        state = json.loads(c.execute('SELECT state_json FROM mistake_dialogue WHERE session_id=?', (sid,)).fetchone()[0])
        assert state['pending_operation']['utterance'] == '答案是-999'
        assert c.execute('SELECT count(*) FROM tutor_turn').fetchone()[0] == 0


@pytest.mark.parametrize('text,owned', [
    ('我不知道为什么错了', True), ('这题我还是不知道哪一步错了', True),
    ('我不清楚错在哪里', True), ('为什么电灯会亮', False), ('我知道为什么错了', False),
    ('妈妈说我不知道为什么错了', False), ('我不知道为什么错了，先关摄像头', False),
    ('先讲别的题', False), ('我不是不知道为什么错了', False),
])
def test_only_current_error_uncertainty_owns_diagnosis_reply(text, owned):
    from home_study_agent.mistake_question_confirmation import relative_diagnosis_reply
    assert relative_diagnosis_reply(text) is owned
