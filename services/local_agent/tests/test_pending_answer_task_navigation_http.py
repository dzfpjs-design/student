"""Real local HTTP/Runtime; synthetic recognition and disposable plans only."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.exam_simulation import ExamSimulationService
from home_study_agent.repository import StudyRepository
from home_study_agent.voice_control_intent import VoiceControlIntent
from test_diagnostic_prompt_http import environment, start
from test_content_free_voice_control_http import task_fixture
from test_natural_task_navigation_http import Offline
from test_voice_runtime_binding import capture, prepare


def setup(client, db):
    sid = start(client)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    repo = task_fixture(db, sid)
    prompt = prepare(client, sid)
    with db.connect() as c:
        assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()[0] == 'waiting_answer'
    return sid, repo, prompt


def say(client, settings, monkeypatch, sid, prompt, text, key, mode):
    body = {'transcript': text, 'client_action_id': key, 'cloud_text_allowed': False}
    if mode == 'voice':
        body['voice_input_id'] = capture(client, settings, monkeypatch, sid, text, prompt, key+'-voice')
    response = client.post(f'/v1/sessions/{sid}/agent-turn', json=body)
    assert response.status_code == 200, response.text
    return response.json()


def execute(client, heard):
    result = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
    assert result.status_code == 200, result.text
    return result.json()


def unchanged_prompt(client, db, sid, prompt):
    current = client.get(f'/v1/sessions/{sid}/diagnostic-prompt').json()['prompt']
    assert (current['prompt_id'], current['item_version'], current['item']) == (
        prompt['prompt_id'], prompt['item_version'], prompt['item'])
    with db.connect() as c:
        assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()[0] == 'waiting_answer'
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id='child-junior-3'").fetchone()[0] == 0


@pytest.mark.parametrize('mode', ['typed', 'voice'])
def test_exact_pause_current_resume_replay_retains_original_pending_question(environment, monkeypatch, mode):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=Offline())) as client:
        sid, repo, prompt = setup(client, db)
        paused = say(client, settings, monkeypatch, sid, prompt, '暂停任务', 'pause-current', mode)
        assert paused['runtime_capability'] == 'task.pause', paused
        result = execute(client, paused)
        assert result['execution']['result']['plan_item_id'] == 'control-task-1'
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
        unchanged_prompt(client, db, sid, prompt)
        heard = say(client, settings, monkeypatch, sid, prompt, '继续当前任务', 'resume-current', mode)
        assert heard['runtime_capability'] == 'task.resume', heard
        execute(client, heard)
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'active'
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'pending'
        execute(client, paused)
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'active'
        unchanged_prompt(client, db, sid, prompt)
        repeat = say(client, settings, monkeypatch, sid, prompt, '再读一遍这道题', 'repeat-original', mode)
        assert repeat['runtime_capability'] == 'diagnostic.replay', repeat
        assert repeat['action']['diagnostic_prompt_id'] == prompt['prompt_id']
        assert repeat['action']['diagnostic_item_version'] == prompt['item_version']
        admitted = client.post(f"/v1/sessions/{sid}/agent-turns/{repeat['agent_turn_id']}/control-admission")
        assert admitted.status_code == 200, admitted.text
        receipt = {'capability': 'diagnostic.replay', 'execution_location': 'client_device_bridge',
            'action_type': 'replay_diagnostic', 'status': 'completed', 'completed_at': '2026-10-09T00:00:00Z',
            'output': {'prompt': prompt}}
        saved = client.put(f"/v1/agent-turns/{repeat['agent_turn_id']}/outcome", json={
            'final_response': '重读原题的合成设备回执。', 'tool_status': 'completed',
            'result': {'execution_receipt': receipt, 'client_bridge_receipt': receipt}})
        assert saved.status_code == 200, saved.text
        unchanged_prompt(client, db, sid, prompt)
    with TestClient(create_app(settings, model_gateway=Offline())) as client:
        unchanged_prompt(client, db, sid, prompt)
        body = {'transcript': '暂停任务', 'client_action_id': 'pause-current', 'cloud_text_allowed': False}
        if mode == 'voice':
            body['voice_input_id'] = paused['action']['voice_input_binding']['voice_input_id']
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json=body)
        assert response.status_code == 200, response.text
        replay = response.json()
        assert replay['agent_turn_id'] == paused['agent_turn_id']
        execute(client, replay)
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'pending'


@pytest.mark.parametrize('text', ['暂停任务', '继续当前任务'])
def test_navigation_without_original_task_does_not_start_next_or_create_result(environment, monkeypatch, text):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=Offline())) as client:
        sid = start(client)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.commit()
        prompt = prepare(client, sid)
        heard = say(client, settings, monkeypatch, sid, prompt, text, 'missing-current-task', 'typed')
        assert heard['runtime_capability'] == 'conversation.respond', heard
        execute(client, heard)
        unchanged_prompt(client, db, sid, prompt)
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM plan_item_event').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


def test_revoked_voice_never_pauses_frozen_task(environment, monkeypatch):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=Offline()), raise_server_exceptions=False) as client:
        sid, repo, prompt = setup(client, db)
        heard = say(client, settings, monkeypatch, sid, prompt, '暂停任务', 'revoked-voice-pause', 'voice')
        voice = heard['action']['voice_input_binding']['voice_input_id']
        with db.connect() as c:
            c.execute("UPDATE voice_input SET status='ignored' WHERE voice_input_id=?", (voice,))
            c.commit()
        response = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert response.status_code == 409, response.text
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'active'
        unchanged_prompt(client, db, sid, prompt)


@pytest.mark.parametrize('text', [
    '不要暂停任务', '不要先暂停任务', '先别暂停任务', '暂停任务，不对，先别暂停',
    '暂停任务后我算出了十二', '我的答案是暂停任务', '题目说“暂停任务”',
    '老师说暂停任务', '如果暂停任务会怎样', '暂停任务是什么意思',
    '不要继续当前任务', '继续当前任务，不对，撤回', '再读一遍这道题是我的答案',
])
@pytest.mark.parametrize('mode', ['typed', 'voice'])
def test_answer_negation_quote_conditional_and_retraction_are_not_task_controls(environment, monkeypatch, text, mode):
    settings, db = environment
    assert VoiceControlIntent.local_task_navigation(text) not in {'pause','current_continue','replay_current'}
    with TestClient(create_app(settings, model_gateway=Offline())) as client:
        sid, repo, prompt = setup(client, db)
        heard = say(client, settings, monkeypatch, sid, prompt, text, 'not-a-control', mode)
        assert heard['runtime_capability'] not in {'task.pause', 'task.resume', 'task.start', 'diagnostic.replay'}, heard
        if heard['runtime_capability'] == 'conversation.respond':
            execute(client, heard)
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'active'
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'pending'
        unchanged_prompt(client, db, sid, prompt)


@pytest.mark.parametrize('mode', ['typed', 'voice'])
@pytest.mark.parametrize('change', ['replacement','source','version','question_version','revoke','new_arrival',
                                   'child','session_closed','write_race','exam','activity'])
def test_pause_uses_only_frozen_task_and_current_input_in_write_transaction(environment, monkeypatch, mode, change):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=Offline()), raise_server_exceptions=False) as client:
        sid, repo, prompt = setup(client, db)
        heard = say(client, settings, monkeypatch, sid, prompt, '暂停任务', 'frozen-pause', mode)
        assert heard['runtime_capability'] == 'task.pause', heard
        if change == 'replacement':
            repo.pause_plan_item('control-task-1', 'separate-pause')
            repo.start_plan_item('control-task-2', 'separate-start')
        elif change == 'new_arrival':
            say(client, settings, monkeypatch, sid, prompt, '不要暂停任务', 'later-retraction', mode)
        elif change == 'exam':
            ExamSimulationService(db).start(sid, 'competition_math', '数学', 'practice', 2, 30, 'later-exam')
        elif change == 'activity':
            AgentRuntimeStore(db).start_companion_activity(sid, 'movement', 'Synthetic stretch', 'later-activity')
        elif change == 'write_race':
            original = StudyRepository.pause_plan_item
            def revoked(self, *args, **kwargs):
                with db.connect() as c:
                    c.execute("UPDATE voice_control_intent_request SET status='cancelled'")
                    c.commit()
                return original(self, *args, **kwargs)
            monkeypatch.setattr(StudyRepository, 'pause_plan_item', revoked)
        else:
            with db.connect() as c:
                sql = {'source': "UPDATE plan_item SET source_ref='changed-original' WHERE plan_item_id='control-task-1'",
                    'version': "UPDATE daily_plan SET version=version+1 WHERE plan_id='control-plan'",
                    'question_version': "UPDATE diagnostic_prompt SET item_version='changed-question'",
                    'revoke': "UPDATE voice_control_intent_request SET status='cancelled'",
                    'child': "UPDATE daily_session SET child_id='child-junior-3' WHERE session_id=?",
                    'session_closed': "UPDATE daily_session SET status='completed' WHERE session_id=?"}[change]
                c.execute(sql, (sid,) if change in {'child','session_closed'} else ())
                c.commit()
        response = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert response.status_code == 409, response.text
        assert repo._get_plan_item_by_id('control-task-2')['status'] == ('active' if change == 'replacement' else 'pending')
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            key = c.execute('SELECT s.idempotency_key FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=?',
                (heard['agent_turn_id'],)).fetchone()[0]
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='paused' AND client_action_id=?", (key,)).fetchone()[0] == 0


@pytest.mark.parametrize('competition', ['active_exam', 'paused_exam', 'activity'])
@pytest.mark.parametrize('text', ['暂停任务', '先暂停任务'])
def test_existing_competing_owner_does_not_pause_schedule(environment, monkeypatch, competition, text):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=Offline())) as client:
        sid, repo, prompt = setup(client, db)
        if competition == 'activity':
            AgentRuntimeStore(db).start_companion_activity(sid, 'movement', 'Synthetic stretch', 'existing-activity')
        else:
            service = ExamSimulationService(db)
            run = service.start(sid, 'competition_math', '数学', 'practice', 2, 30, 'existing-exam')
            if competition == 'paused_exam':
                service.pause(run['run_id'])
        original_status = repo._get_plan_item_by_id('control-task-1')['status']
        heard = say(client, settings, monkeypatch, sid, prompt, text, 'competing-owner-pause', 'typed')
        assert heard['runtime_capability'] not in {'task.pause','task.resume','task.start'}, heard
        execute(client, heard)
        assert repo._get_plan_item_by_id('control-task-1')['status'] == original_status
        unchanged_prompt(client, db, sid, prompt)


@pytest.mark.parametrize('lost', [False, True])
def test_pause_receipt_replay_cannot_pause_replacement_task(environment, monkeypatch, lost):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=Offline()), raise_server_exceptions=False) as client:
        sid, repo, prompt = setup(client, db)
        heard = say(client, settings, monkeypatch, sid, prompt, '暂停任务', 'one-pause', 'typed')
        if lost:
            original = StudyRepository.pause_plan_item
            def lost_response(self, *args, **kwargs):
                result = original(self, *args, **kwargs)
                raise OSError('synthetic response lost after actual pause')
            with monkeypatch.context() as local:
                local.setattr(StudyRepository, 'pause_plan_item', lost_response)
                assert client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute").status_code == 500
        else:
            execute(client, heard)
        repo.start_plan_item('control-task-2', 'replacement-after-pause')
        execute(client, heard)
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'active'
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='paused' AND plan_item_id='control-task-1'").fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='paused' AND plan_item_id='control-task-2'").fetchone()[0] == 0
        unchanged_prompt(client, db, sid, prompt)


@pytest.mark.parametrize('change', ['source','new_pause','replacement','revoke','new_arrival','child'])
def test_current_resume_cannot_reselect_or_reuse_old_pause(environment, monkeypatch, change):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=Offline()), raise_server_exceptions=False) as client:
        sid, repo, prompt = setup(client, db)
        paused = say(client, settings, monkeypatch, sid, prompt, '暂停任务', 'pause-before-resume', 'typed')
        execute(client, paused)
        resume = say(client, settings, monkeypatch, sid, prompt, '继续当前任务', 'frozen-current-resume', 'typed')
        assert resume['runtime_capability'] == 'task.resume', resume
        if change in {'new_pause','replacement'}:
            if change == 'new_pause':
                repo.resume_plan_item('control-task-1', 'separate-resume')
                repo.pause_plan_item('control-task-1', 'separate-new-pause')
            else:
                repo.start_plan_item('control-task-2', 'separate-task-start')
        elif change == 'new_arrival':
            say(client, settings, monkeypatch, sid, prompt, '不要继续当前任务', 'later-resume-retraction', 'typed')
        else:
            with db.connect() as c:
                if change == 'source':
                    c.execute("UPDATE plan_item SET source_ref='changed-paused-source' WHERE plan_item_id='control-task-1'")
                elif change == 'revoke':
                    c.execute("UPDATE voice_control_intent_request SET status='cancelled'")
                else:
                    c.execute("UPDATE daily_session SET child_id='child-junior-3' WHERE session_id=?", (sid,))
                c.commit()
        result = client.post(f"/v1/agent-turns/{resume['agent_turn_id']}/execute")
        assert result.status_code == 409, result.text
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
        assert repo._get_plan_item_by_id('control-task-2')['status'] == ('active' if change == 'replacement' else 'pending')
        with db.connect() as c:
            key = c.execute('SELECT s.idempotency_key FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=?',
                (resume['agent_turn_id'],)).fetchone()[0]
            assert c.execute('SELECT COUNT(*) FROM plan_item_event WHERE client_action_id=?', (key,)).fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
