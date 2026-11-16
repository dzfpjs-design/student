"""One unchanged real plan across school, PET and math; inference is synthetic."""
import json
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest

from home_study_agent.app import create_app
from test_primary_mixed_daily_budget import mixed_day, flow, CHILD
from test_voice_runtime_binding import capture, interpret
from test_writing_stage_review_consumers import advance_clock
from test_content_free_voice_control_http import ControlGateway
from test_cloud_first_agent import Planner


class MixedGateway(ControlGateway):
    async def complete(self, request):
        if request.request_type == 'agent_plan':
            self.requests.append(request)
            if self.control == 'mistake.dialogue':
                return await Planner(self.control, {'utterance': self.quote}, self.quote,
                    objective_type='learning').complete(request)
            return await Planner(self.control, {}, self.quote).complete(request)
        return await super().complete(request)


def supply_reviewed_signal(case, client, monkeypatch, prompt, key):
    from test_pet_media_evidence import wav_file
    path = case['settings'].data_dir / 'synthetic-listening.wav'
    wav_file(path)
    pcm = path.read_bytes()
    monkeypatch.setattr('home_study_agent.pet_material_supply.render_original_practice',
        lambda text: (pcm, 'synthetic_test_signal_not_speech_quality'))
    unlock = client.post('/v1/parent/unlock', json={'pin': '8264'})
    assert unlock.status_code == 200, unlock.text
    headers = {'X-HSC-Parent': unlock.json()['parent_token']}
    base = f"/v1/sessions/{case['sid']}/pet-media"
    request = dict(item_version=prompt['item_version'], client_action_id=key+'-material')
    url = base + f"/prompt/{prompt['prompt_id']}/materialize"
    created = client.post(url, json=request, headers=headers)
    assert created.status_code == 200, created.text
    assert client.post(url, json=request, headers=headers).json() == created.json()
    material = created.json()['material']
    bound = client.post(base+'/listening/begin', json=dict(prompt_id=prompt['prompt_id'],
        item_version=prompt['item_version'], artifact_id=material['artifact_id'], client_action_id=key+'-bind'))
    assert bound.status_code == 200, bound.text
    media = bound.json()['media']
    audition = client.get(base+f"/{media['media_id']}/review-audio", headers=headers,
        params={'audio_sha256': media['audio']['sha256']})
    assert audition.status_code == 200 and audition.content == pcm
    approved = client.post(base+f"/{media['media_id']}/source-review", headers=headers, json=dict(
        audio_sha256=media['audio']['sha256'], operation='approve', client_action_id=key+'-approve'))
    assert approved.status_code == 200, approved.text


@pytest.mark.parametrize('restart_midway', [False, True])
@pytest.mark.parametrize('review_correct', [False, True])
def test_three_lanes_keep_actual_tasks_answers_and_recovery(mixed_day, monkeypatch, restart_midway, review_correct, paper_hook=None, followup_hook=None):
    case = mixed_day
    case['finish_client']()
    gateway = MixedGateway('')
    at = datetime(2026, 9, 14, 8, tzinfo=timezone.utc)
    active_sid = case['sid']
    with ExitStack() as sessions:
        client = sessions.enter_context(TestClient(create_app(case['settings'], model_gateway=gateway)))
        def post(path, body=None):
            response = client.post(path, json=body or {})
            assert response.status_code in (200, 201), response.text
            return response.json()

        def say(text, key, control, prompt=None):
            gateway.quote, gateway.control = text, control
            voice = capture(client, case['settings'], monkeypatch, active_sid, text, prompt, key+'-audio')
            turn = interpret(client, active_sid, text, key, voice)
            return turn

        def execute(text, key, control, prompt=None):
            turn = say(text, key, control, prompt)
            assert turn.get('runtime_capability') == control, {k: turn.get(k) for k in (
                'intent', 'action', 'response_text', 'runtime_capability')}
            result = post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
            return result['execution']['result']

        post(f"/v1/sessions/{case['sid']}/transition", dict(expected_state='plan_review', target_state='study',
            reason='按今晚的计划学习。', client_action_id='mixed-voice-study'))
        completed = []
        prompts = []
        attempts = []
        math_attempt = None
        for index, item in enumerate(sorted(case['plan']['plan_items'], key=lambda p: p['position'])):
            key = f'mixed-voice-{index}'
            if item['source_type'] == 'planned_break':
                execute('我先休息一会儿', key+'-break', 'break.begin')
                at += timedelta(minutes=item['expected_minutes'])
                advance_clock(monkeypatch, at)
                execute('休息好了，继续学习', key+'-return', 'break.resume')
                continue
            started = execute('开始下一项', key+'-start', 'task.start')
            if started['status'] == 'awaiting_resources':
                assert item['source_type'] == 'pet_daily', started
                retained = started['diagnostic_prompt']['prompt']
                with case['db'].connect() as c:
                    waiting = c.execute('SELECT status,started_at FROM plan_item WHERE plan_item_id=?',
                        (item['plan_item_id'],)).fetchone()
                    assert tuple(waiting) == ('pending', None)
                # Explicit synthetic source approval is a fixture boundary, not
                # proof of real listening content, playback or a child's skill.
                supply_reviewed_signal(case, client, monkeypatch, retained, key)
                started = execute('继续原任务', key+'-material-ready', 'task.resume', retained)
                assert started['diagnostic_prompt']['prompt']['prompt_id'] == retained['prompt_id']
            assert started['status'] == 'active', (item['source_type'], started)
            assert started['plan_item_id'] == item['plan_item_id']
            if item['source_type'] == 'school_homework' and paper_hook:
                paper_hook(dict(case, client=client, post=post, say=say, gateway=gateway), item)
            prompt = (started.get('diagnostic_prompt') or {}).get('prompt')
            if item['source_type'] in {'pet_daily', 'competition_math'}:
                assert prompt, (item['source_type'], started)
                with case['db'].connect() as c:
                    original = c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?',
                        (prompt['diagnostic_item_id'],)).fetchone()
                    rubric = json.loads(original['expected_answer_json'])
                if prompt['item'].get('pet_audio_modality') == 'listening':
                    media_service = client.app.state.pet_media
                    media = media_service.list_for_prompt(case['sid'], prompt_id=prompt['prompt_id'],
                        item_version=prompt['item_version'])['listening'][0]
                    clock = [1000]
                    monkeypatch.setattr('home_study_agent.pet_media_evidence._clock_ms', lambda: clock[0])
                    media = media_service.control_listening(case['sid'], media_id=media['media_id'],
                        generation=media['generation'], expected_revision=media['revision'],
                        command='resume', client_action_id=key+'-play')
                    for event, frame, elapsed in [('started', 0, 0), ('ended', media['audio']['frames'], 1000)]:
                        clock[0] = 1000 + elapsed
                        media = media_service.record_playback(case['sid'], media_id=media['media_id'],
                            generation=media['generation'], expected_revision=media['revision'], event=event,
                            audio_sha256=media['audio']['sha256'], frame_position=frame,
                            monotonic_ms=clock[0], client_action_id=key+'-'+event)
                    assert media['status'] == 'playback_finished'
                    assert media['qualification']['skill_qualified'] is False
                    if restart_midway:
                        sessions.close()
                        client = sessions.enter_context(TestClient(create_app(case['settings'], model_gateway=gateway)))
                        current = client.get(f"/v1/sessions/{case['sid']}/diagnostic-prompt",
                            params={'prompt_id': prompt['prompt_id']}).json()
                        assert current['prompt']['prompt_id'] == prompt['prompt_id']
                        restored_media = client.app.state.pet_media.read(case['sid'], media_id=media['media_id'])
                        assert restored_media['status'] == 'playback_finished'
                        with case['db'].connect() as c:
                            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',
                                (item['plan_item_id'],)).fetchone()[0] == 'paused'
                            assert not c.execute('SELECT 1 FROM diagnostic_attempt WHERE session_id=?', (case['sid'],)).fetchone()
                        resumed = execute('刚才那道英语题我接着做', key+'-recover', 'task.resume', prompt)
                        assert resumed['status'] == 'active'
                        assert resumed['plan_item_id'] == item['plan_item_id']
                        assert resumed['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']
                    text = '答案是 We meet at the front of the library.'
                else:
                    assert rubric.get('exact_answers'), (item['source_type'], rubric)
                    text = '答案是-999'
                turn = say(text, key+'-answer', 'task.complete', prompt)
                assert turn['action']['type'] == 'submit_diagnostic_answer', turn['action']
                answered = post(f"/v1/sessions/{case['sid']}/diagnostic-prompt/answer", dict(
                    prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
                    answer=turn['action']['answer'], authorized_turn_id=turn['agent_turn_id'],
                    client_action_id=key+'-submit'))
                assert answered['status'] == 'answered', answered
                assert answered['attempt']['diagnostic_item_id'] == prompt['diagnostic_item_id']
                if item['source_type'] == 'competition_math':
                    assert answered['attempt']['correctness'] == 0
                    math_attempt = answered['attempt']
                attempts.append(answered['attempt'])
                prompts.append(prompt)
            at += timedelta(minutes=min(5, item['expected_minutes']))
            advance_clock(monkeypatch, at)
            finished = execute('这项做完了', key+'-complete', 'task.complete', prompt)
            assert finished['status'] == 'completed', (item['source_type'], finished)
            assert finished['plan_item_id'] == item['plan_item_id']
            completed.append(item['source_type'])
        assert {'school_homework', 'pet_daily', 'competition_math'} <= set(completed)
        assert len({p['prompt_id'] for p in prompts}) == 2
        assert len({a['attempt_id'] for a in attempts}) == 2
        post(f"/v1/sessions/{case['sid']}/transition", dict(expected_state='study', target_state='reflection',
            reason='今天先到这里，错的题之后再复查。', client_action_id='mixed-reflect'))
        post(f"/v1/sessions/{case['sid']}/reflection/complete", dict(learned_text='', difficult_text='',
            client_action_id='mixed-close'))
        summary = client.get(f"/v1/sessions/{case['sid']}/learning-summary").json()
        assert math_attempt is not None
        with case['db'].connect() as c:
            math_mistake = c.execute(
                'SELECT * FROM mistake_case WHERE child_id=? AND source_evidence_id=?',
                (CHILD, math_attempt['evidence_id']),
            ).fetchone()
            assert math_mistake and math_mistake['submitted_answer'] == '-999'
            math_mistake_id = math_mistake['mistake_id']
        followups = [r for r in summary['remediation']
                     if r.get('source_type') == 'mistake_review'
                     and r.get('source_ref') == math_mistake_id]
        assert followups, summary['remediation']
        with case['db'].connect() as c:
            history = [tuple(r) for r in c.execute('SELECT * FROM diagnostic_attempt WHERE session_id=? ORDER BY attempt_id',
                (case['sid'],))]
            ended = c.execute('SELECT ended_at FROM daily_session WHERE session_id=?', (case['sid'],)).fetchone()[0]
    with TestClient(create_app(case['settings'], model_gateway=gateway)) as reopened:
        workspace = reopened.get(f"/v1/sessions/{case['sid']}").json()
        assert all(p['status'] == 'completed' for p in workspace['plan_items'])
        with case['db'].connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?', (case['sid'],)).fetchone()[0] == 2
            assert not c.execute('SELECT 1 FROM diagnostic_attempt WHERE child_id!=?', (CHILD,)).fetchone()
        recovered = reopened.get(f"/v1/sessions/{case['sid']}/learning-summary").json()
        assert recovered['remediation'] == summary['remediation']
        client = reopened
        advance_clock(monkeypatch, datetime.fromisoformat(ended) + timedelta(days=8))
        tomorrow = post('/v1/sessions', dict(child_id=CHILD, client_action_id='mixed-followup-day'))['session_id']
        response = client.put(f'/v1/sessions/{tomorrow}/planning-context', json=dict(energy_level='medium',
            fixed_commitment_minutes=0, client_action_id='mixed-followup-context'))
        assert response.status_code == 200, response.text
        post(f'/v1/sessions/{tomorrow}/transition', dict(expected_state='check_in', target_state='classroom_recall',
            reason='今天复习。', client_action_id='mixed-followup-recall'))
        post(f'/v1/sessions/{tomorrow}/recalls/none', dict(client_action_id='mixed-no-recall'))
        post(f'/v1/sessions/{tomorrow}/transition', dict(expected_state='classroom_recall', target_state='homework_capture',
            reason='今天没有新增作业。', client_action_id='mixed-followup-homework'))
        post(f'/v1/sessions/{tomorrow}/homework/none', dict(client_action_id='mixed-no-homework'))
        next_plan = post(f'/v1/sessions/{tomorrow}/plan/generate', dict(client_action_id='mixed-followup-plan'))
        review_ids = {r['source_ref'] for r in followups}
        scheduled = [p for p in next_plan['plan_items']
                     if p['source_type'] == 'mistake_review' and p['source_ref'] in review_ids]
        assert scheduled and all(p['status'] == 'pending' and p['started_at'] is None for p in scheduled)
        with case['db'].connect() as c:
            assert [tuple(r) for r in c.execute('SELECT * FROM diagnostic_attempt WHERE session_id=? ORDER BY attempt_id',
                (case['sid'],))] == history
            parallel = c.execute(
                'SELECT status FROM review_task WHERE child_id=? AND source_evidence_id=?',
                (CHILD, math_attempt['evidence_id']),
            ).fetchone()
            assert not parallel or parallel['status'] == 'cancelled', (
                'A confirmed mistake must not leave a second generic review loop active'
            )
        active_sid = tomorrow
        post(f'/v1/sessions/{tomorrow}/transition', dict(expected_state='plan_review', target_state='study',
            reason='先复查之前答错的题。', client_action_id='mixed-followup-study'))
        first = min(next_plan['plan_items'], key=lambda p: p['position'])
        if followup_hook:
            followup_hook(dict(case, client=client, post=post, say=say, execute=execute,
                               next_sid=tomorrow, next_plan=next_plan, first=first))
            return
        assert first['plan_item_id'] in {p['plan_item_id'] for p in scheduled}, [
            (p['source_type'], p['source_ref']) for p in next_plan['plan_items']]
        started = execute('开始今天的复查', 'mixed-review-start', 'task.start')
        assert started['status'] == 'active' and started['plan_item_id'] == first['plan_item_id']
        assert started['mistake_dialogue']['mistake_id'] == math_mistake_id
        current = client.get(f'/v1/sessions/{tomorrow}/mistake-dialogue').json()['dialogue']
        assert current['mistake_id'] == math_mistake_id
        assert current['state']['submitted_answer'] == '-999'
        assert current['state']['question_text']
        with case['db'].connect() as c:
            follow_plan_id = c.execute('SELECT plan_id FROM plan_item WHERE plan_item_id=?',
                (first['plan_item_id'],)).fetchone()[0]
            untouched = [tuple(r) for r in c.execute(
                'SELECT plan_item_id,status,started_at FROM plan_item '
                'WHERE plan_id=? AND plan_item_id!=? ORDER BY plan_item_id',
                (follow_plan_id, first['plan_item_id']))]
            assert c.execute('SELECT source_evidence_id FROM mistake_case WHERE mistake_id=?',
                             (math_mistake_id,)).fetchone()[0] == math_attempt['evidence_id']
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?',
                             (case['sid'],)).fetchone()[0] == 2
            assert all(status == 'pending' and started_at is None
                       for _, status, started_at in untouched)
    with TestClient(create_app(case['settings'], model_gateway=gateway)) as restored:
        recovered_dialogue = restored.get(f'/v1/sessions/{tomorrow}/mistake-dialogue').json()['dialogue']
        assert recovered_dialogue['mistake_id'] == math_mistake_id
        assert recovered_dialogue['state']['submitted_answer'] == '-999'
        with case['db'].connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?',
                             (tomorrow,)).fetchone()[0] == 0
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',
                             (first['plan_item_id'],)).fetchone()[0] == 'paused'
            assert [tuple(r) for r in c.execute(
                'SELECT plan_item_id,status,started_at FROM plan_item '
                'WHERE plan_id=? AND plan_item_id!=? ORDER BY plan_item_id',
                (follow_plan_id, first['plan_item_id']))] == untouched
    controls = [r for r in gateway.requests if r.request_type == 'voice_interaction']
    assert controls
    for request in controls:
        payload = json.loads(request.messages[-1]['content'])
        assert set(payload) == {'transcript', 'state'}
        assert all(type(value) is bool for value in payload['state'].values())
        assert request.evidence_refs == ()
        serialized = json.dumps(request.messages, ensure_ascii=False)
        assert all(p['prompt_id'] not in serialized and p['item']['prompt'] not in serialized for p in prompts)
