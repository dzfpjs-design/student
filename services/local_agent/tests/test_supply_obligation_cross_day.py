"""A real unfulfilled supply promise must survive the day boundary."""
import json
import pytest
from datetime import datetime, timedelta, timezone

from home_study_agent.repository import StudyRepository
from test_supply_gap_resume_http import supplied, CHILD
from test_writing_stage_review_consumers import advance_clock


def close_and_plan(supplied, monkeypatch, *, source_pattern='pet-writing-gap:%', target_child=CHILD, before_generate=None):
    case = supplied
    client, post, sid = case['client'], case['post'], case['sid']
    with case['db'].connect() as c:
        original = dict(c.execute("""SELECT * FROM plan_constraint_decision
            WHERE plan_id=(SELECT plan_id FROM daily_plan WHERE session_id=?)
            AND source_ref LIKE ? AND decision='deferred'
            ORDER BY rowid LIMIT 1""", (sid, source_pattern)).fetchone())
        attempts = c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]
    assert case['published']['eligible']
    post(client, f'/v1/sessions/{sid}/transition', {
        'expected_state': 'study', 'target_state': 'reflection',
        'reason': 'The child is done for today; the writing promise is still unfulfilled.',
        'client_action_id': 'cross-day-reflection'})
    post(client, f'/v1/sessions/{sid}/reflection/complete', {
        'learned_text': '', 'difficult_text': '', 'client_action_id': 'cross-day-close'})
    with case['db'].connect() as c:
        closed = dict(c.execute('SELECT * FROM daily_session WHERE session_id=?', (sid,)).fetchone())
        assert closed['status'] == 'completed'
        assert dict(c.execute('SELECT * FROM plan_constraint_decision WHERE decision_id=?',
            (original['decision_id'],)).fetchone()) == original
    clock = advance_clock(monkeypatch, datetime.fromisoformat(closed['ended_at']) + timedelta(days=1))
    case['cross_day_clock'] = clock
    from home_study_agent import learning_input_replan
    monkeypatch.setattr(learning_input_replan, 'utc_now', lambda: clock.now(timezone.utc))
    token = post(client, '/v1/parent/unlock', {'pin': '7391'})['parent_token']
    client.headers['X-HSC-Parent'] = token
    next_sid = post(client, '/v1/sessions', {
        'child_id': target_child, 'client_action_id': 'cross-day-next-session'})['session_id']
    case['next_sid'] = next_sid
    context = client.put(f'/v1/sessions/{next_sid}/planning-context', json={
        'energy_level': 'medium', 'fixed_commitment_minutes': 0,
        'client_action_id': 'cross-day-planning-context'})
    assert context.status_code == 200, context.text
    # Initial declarations only; plan, obligation, prompt and event are real writes.
    with case['db'].connect() as c:
        c.execute("""UPDATE daily_session SET state='homework_capture',
            homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?""", (next_sid,))
        c.commit()
    pending = client.get(f'/v1/sessions/{next_sid}').json()
    for item in pending['carryover_candidates']:
        post(client, f"/v1/sessions/{next_sid}/deferred-tasks/{item['source_plan_item_id']}/review", {
            'decision': 'continue_today', 'reason': '今天继续原来延期的校内任务。',
            'client_action_id': 'cross-day-review-' + item['source_plan_item_id']})
    if before_generate:
        before_generate(case, next_sid)
    post(client, f'/v1/sessions/{next_sid}/plan/generate', {'client_action_id': 'cross-day-next-plan'})
    restored = StudyRepository(case['db']).get_workspace(next_sid)
    with case['db'].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == attempts
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?', (next_sid,)).fetchone()[0] == 0
        assert dict(c.execute('SELECT * FROM plan_constraint_decision WHERE decision_id=?',
            (original['decision_id'],)).fetchone()) == original
    return next_sid, original, restored, attempts


def test_unfulfilled_published_supply_obligation_survives_actual_day_close(supplied, monkeypatch):
    next_sid, original, restored, attempts = close_and_plan(supplied, monkeypatch)
    current_promises = json.dumps({key: restored.get(key) for key in
        ('constraint_decisions', 'supply_obligations', 'carryover_candidates', 'carryover_decisions')})
    assert original['decision_id'] in current_promises, (
        'Next-day plan lost the exact unfulfilled obligation; selecting another PET '
        'task with the same skill is not a durable carryover receipt.',
        original['decision_id'], current_promises[:1600])


def interpret_next_day(case, monkeypatch, sid, key='next-day-resume', *, quote=None, prompt=None):
    from test_voice_runtime_binding import capture, interpret
    quote = quote or '昨天缺题的写作练习已经补好了，请继续原来的任务。'
    case['speech']['quote'] = quote
    voice = capture(case['client'], case['settings'], monkeypatch, sid, quote, prompt, key=key+'-voice')
    turn = interpret(case['client'], sid, quote, key, voice)
    assert turn.get('runtime_capability') == 'task.resume', turn
    return turn


def resume_next_day(case, monkeypatch, sid, key='next-day-resume', *, quote=None, prompt=None):
    turn = interpret_next_day(case, monkeypatch, sid, key, quote=quote, prompt=prompt)
    return turn, case['client'].post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute", json={})


def test_cross_day_actual_voice_starts_original_no_duplicate_and_reopens(supplied, monkeypatch):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    case = supplied
    sid, root, workspace, attempts = close_and_plan(case, monkeypatch)
    writing = [p for p in workspace['plan_items'] if p['source_type']=='pet_daily' and p['task_type']=='writing']
    assert writing == [], 'New plan must not allocate a random replacement alongside the old writing promise'
    case['post'](case['client'], f'/v1/sessions/{sid}/transition', dict(expected_state='plan_review',
        target_state='study', reason='Accept next day plan', client_action_id='next-day-study'))
    turn, response = resume_next_day(case, monkeypatch, sid)
    assert response.status_code == 200, response.text
    with case['db'].connect() as c:
        task = dict(c.execute('''SELECT pi.* FROM plan_item pi JOIN daily_plan dp USING(plan_id)
            WHERE dp.session_id=? AND pi.source_type='pet_daily' AND pi.task_type='writing' ''', (sid,)).fetchone())
        assert task['source_ref'] == case['published']['diagnostic_item_id'] and task['status']=='active'
        prompt = dict(c.execute('SELECT * FROM diagnostic_prompt WHERE origin_session_id=?', (sid,)).fetchone())
        assert prompt['diagnostic_item_id'] == task['source_ref'] and prompt['status']=='waiting_answer'
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == attempts
        started = c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (task['plan_item_id'],)).fetchone()[0]
        assert started == 1
    current = StudyRepository(case['db']).get_workspace(sid)
    promised = next(o for o in current['supply_obligations'] if o.get('root_obligation_id')==root['decision_id'])
    assert promised['plan_item_id'] == task['plan_item_id'] and promised['status']=='active'
    replay = case['client'].post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute", json={})
    assert replay.status_code == 200, replay.text
    case['client'].__exit__(None, None, None)
    with TestClient(create_app(case['settings'], model_gateway=case['gateway']),
                    headers={'X-HSC-Session': case['settings'].session_token},
                    client=('127.0.0.1', 41505)) as client:
        restored = client.get(f'/v1/sessions/{sid}')
        assert restored.status_code == 200, restored.text
        with case['db'].connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (task['plan_item_id'],)).fetchone()[0] == started
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (task['plan_item_id'],)).fetchone()[0] == 'paused'
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == attempts
            assert c.execute('SELECT diagnostic_item_id FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()[0] == task['source_ref']


@pytest.mark.parametrize('supplied', ['listening_wait'], indirect=True)
@pytest.mark.parametrize('fault', ['none', 'cancel_original'])
def test_placed_audio_wait_carries_exact_original_not_a_skill_replacement(supplied, monkeypatch, fault):
    from test_supply_gap_listening_wait import publish_listening
    from test_supply_gap_resume_http import execute
    from test_pet_listening_resource_admission import bind_audio
    case = supplied
    published = publish_listening(case)
    case['speech']['selector'] = {'subject': None, 'task_kind': 'listening', 'relative_order': None}
    first_turn = case['say']('original-listening-wait', '题目补好了，继续原来的听力练习。')
    response = execute(case, first_turn)
    assert response.status_code == 200, response.text
    first = response.json()['execution']['result']
    assert first['status'] == 'awaiting_resources'
    original_prompt = first['diagnostic_prompt']['prompt']
    with case['db'].connect() as c:
        original_task = dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?',
            (first['plan_item_id'],)).fetchone())
        assert original_task['status'] == 'pending' and original_task['started_at'] is None
        pending_ids = [r[0] for r in c.execute('''SELECT pi.plan_item_id FROM plan_item pi
            JOIN daily_plan dp USING(plan_id) WHERE dp.session_id=? AND pi.status='pending' ''', (case['sid'],))]
    # Real day-end requires explicit postponement; missing audio is not completion.
    for pid in pending_ids:
        case['post'](case['client'], f'/v1/plan-items/{pid}/reschedule', {
            'reason': '今天先到这里；听力原音还没有准备好，原任务延期。',
            'client_action_id': 'explicit-day-end-' + pid})
    with case['db'].connect() as c:
        original_task = dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?',
            (first['plan_item_id'],)).fetchone())
        assert original_task['status'] == 'rescheduled'
    sid, root, workspace, attempts = close_and_plan(case, monkeypatch, source_pattern='pet-skill-gap:listening')
    obligation = next(o for o in workspace['supply_obligations'] if o.get('root_obligation_id') == root['decision_id'])
    assert obligation['carryover_kind'] == 'explicitly_deferred'
    case['post'](case['client'], f'/v1/sessions/{sid}/transition', dict(expected_state='plan_review',
        target_state='study', reason='Accept next day plan', client_action_id='next-listening-study'))
    if fault == 'cancel_original':
        turn = interpret_next_day(case, monkeypatch, sid,
            quote='昨天的原听力题还没有做，请继续原来的听力练习。')
        cancelled = case['post'](case['client'], f"/v1/sessions/{case['sid']}/diagnostic-prompt/cancel", {
            'prompt_id': original_prompt['prompt_id'], 'item_version': original_prompt['item_version']})
        assert cancelled['status'] == 'cancelled'
        refused = case['client'].post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute", json={})
        assert refused.status_code == 409, refused.text
        with case['db'].connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?', (sid,)).fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == attempts
        current = case['client'].get(f'/v1/sessions/{sid}').json()
        blocked = next(o for o in current['supply_obligations'] if o.get('root_obligation_id') == root['decision_id'])
        assert blocked['status'] == 'source_changed'
        return
    _, response = resume_next_day(case, monkeypatch, sid, quote='昨天的原听力题还没有做，请继续原来的听力练习。')
    assert response.status_code == 200, response.text
    waiting = response.json()['execution']['result']
    assert waiting['status'] == 'awaiting_resources'
    prompt = waiting['diagnostic_prompt']['prompt']
    assert prompt['diagnostic_item_id'] == original_prompt['diagnostic_item_id'] == published['diagnostic_item_id']
    assert prompt['item_version'] == original_prompt['item_version']
    assert prompt['prompt_id'] != original_prompt['prompt_id']
    assert waiting['plan_item_id'] != first['plan_item_id']
    with case['db'].connect() as c:
        assert dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?',
            (first['plan_item_id'],)).fetchone()) == original_task
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == attempts
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'",
            (waiting['plan_item_id'],)).fetchone()[0] == 0
    media, path = bind_audio(case['client'], case['db'], case['tmp_path'], sid, prompt,
        approved=False, key='next-day-original-audio')
    # Simulated test-parent reference attestation; synthetic signal, no acoustic/content proof.
    reviewed = case['post'](case['client'], f"/v1/sessions/{sid}/pet-media/{media['media_id']}/source-review", {
        'audio_sha256': media['audio']['sha256'], 'operation': 'approve', 'full_spoken_reference': True, 'client_action_id': 'next-audio-parent-review'})
    assert reviewed['media']['source_reviewed']
    _, response = resume_next_day(case, monkeypatch, sid, key='next-day-audio-ready',
        quote='原音已经补好，请继续昨天那道听力题。', prompt=prompt)
    assert response.status_code == 200, response.text
    started = response.json()['execution']['result']
    assert started['status'] == 'active' and started['plan_item_id'] == waiting['plan_item_id']
    assert started['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']
    # Actual HTTP audio delivery and fresh renderer-protocol receipts. This
    # tests the persisted protocol, not a speaker/microphone acoustic result.
    import time
    from test_pet_media_http import control, event
    base = f'/v1/sessions/{sid}/pet-media'
    http = (case['client'], None, base)
    row = case['client'].get(base + f"/{media['media_id']}").json()['media']
    response = case['client'].get(base + f"/{media['media_id']}/audio", params={'audio_sha256': row['audio']['sha256']})
    assert response.status_code == 200 and response.content == path.read_bytes()
    row = control(http, row, key='next-day-play-control')
    response = event(http, row, 'started', 0, 1000, 'next-day-play-start')
    assert response.status_code == 200, response.text
    row = response.json()['media']
    time.sleep(1.05)
    response = event(http, row, 'ended', 16000, 2000, 'next-day-play-end')
    assert response.status_code == 200, response.text
    assert response.json()['media']['qualification']['listening_delivery_qualified']
    assert not response.json()['media']['qualification']['skill_qualified']
    with case['db'].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == attempts
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'",
            (waiting['plan_item_id'],)).fetchone()[0] == 1
        plays = c.execute("SELECT COUNT(*) FROM pet_media_event WHERE operation='playback'").fetchone()[0]
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    case['client'].__exit__(None, None, None)
    with TestClient(create_app(case['settings'], model_gateway=case['gateway']),
                    headers={'X-HSC-Session': case['settings'].session_token},
                    client=('127.0.0.1', 41509)) as reopened:
        workspace = reopened.get(f'/v1/sessions/{sid}')
        assert workspace.status_code == 200, workspace.text
        recovered = reopened.get(base + f"/{media['media_id']}")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()['media']['qualification']['listening_delivery_qualified']
        for _ in range(2):
            assert reopened.get(f'/v1/sessions/{sid}/diagnostic-prompt').status_code == 200
        with case['db'].connect() as c:
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (waiting['plan_item_id'],)).fetchone()[0] == 'paused'
            assert c.execute("SELECT COUNT(*) FROM pet_media_event WHERE operation='playback'").fetchone()[0] == plays
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == attempts
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?', (sid,)).fetchone()[0] == 1
