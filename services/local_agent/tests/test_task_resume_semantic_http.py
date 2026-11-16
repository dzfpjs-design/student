"""Synthetic ASR and controlled intent; real arrival, journal, dispatcher and SQLite."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelResult
from test_diagnostic_prompt_http import environment, OTHER
from test_task_resume_voice_http import ControlGateway, paused_case, hear
from test_voice_runtime_binding import capture, interpret
from test_writing_stage_reviews import writing_chain


class SelectorGateway(ControlGateway):
    selector = None

    async def complete(self, request):
        result = await super().complete(request)
        value = json.loads(result.content)
        if self.control == 'task.resume' and self.selector is not None:
            value['selector'] = self.selector
        return ModelResult(content=json.dumps(value), provider='test', model='controlled-selector', latency_ms=1)


def two_paused(client, settings, db, monkeypatch, gateway, same_subject=False):
    sid, repo, prompt = paused_case(client, settings, db, monkeypatch, gateway)
    # Only synthetic plan metadata: actual pause/resume still use product writes.
    with db.connect() as c:
        c.execute("UPDATE plan_item SET task_type='review' WHERE plan_item_id='control-task-1'")
        c.execute("UPDATE plan_item SET subject=?,title='PRIVATE ENGLISH WORK' WHERE plan_item_id='control-task-2'",
                  ('数学' if same_subject else '英语',))
        c.commit()
    repo.start_plan_item('control-task-2', 'second-start')
    repo.pause_plan_item('control-task-2', 'second-pause')
    return sid, repo, prompt


@pytest.mark.parametrize('quote,subject,item', [
    ('数学那项', 'math', 'control-task-1'),
    ('先继续英语', 'english', 'control-task-2'),
    ('先做数学的那项', 'math', 'control-task-1'),
    ('复习那项我先接着做', None, 'control-task-1'),
])
def test_natural_subject_resume_after_offer_restart(environment, monkeypatch, quote, subject, item):
    settings, db = environment
    gateway = SelectorGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, repo, prompt = two_paused(client, settings, db, monkeypatch, gateway)
        offer = hear(client, settings, monkeypatch, gateway, sid, prompt, '停下的任务接着做', 'offer-natural', 'task.resume')
        assert offer['action']['source'] == 'task_resume_clarification'
        assert client.post(f"/v1/agent-turns/{offer['agent_turn_id']}/execute").status_code == 200
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        gateway.selector = {'subject': subject, 'task_kind': 'review' if subject is None else None, 'relative_order': None}
        turn = hear(client, settings, monkeypatch, gateway, sid, prompt, quote, 'select-natural', 'task.resume')
        assert turn['action']['type'] == 'resume_paused_task', turn
        url = f"/v1/agent-turns/{turn['agent_turn_id']}/execute"
        result = client.post(url)
        assert result.status_code == 200, result.text
        assert result.json()['execution']['result']['plan_item_id'] == item
        assert repo._get_plan_item_by_id(item)['status'] == 'active'
        assert turn['action']['voice_input_binding']['question_reference']['binding']['prompt_id'] == prompt['prompt_id']
    calls = len(gateway.requests)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        assert client.post(url).status_code == 200
        assert len(gateway.requests) == calls
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    for request in gateway.requests:
        text = json.dumps(request.messages, ensure_ascii=False)
        assert all(secret not in text for secret in (sid, prompt['prompt_id'], prompt['item']['prompt'],
                                                     'control-task-1', 'PRIVATE ENGLISH WORK', 'Synthetic task'))
        assert set(json.loads(request.messages[-1]['content'])) == {'transcript', 'state'}


def test_recent_review_uses_captured_original_not_most_recent_pause(writing_chain, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from home_study_agent.config import Settings
    from home_study_agent.repository import StudyRepository
    from test_agent_continuation import Gateway
    from test_writing_stage_reviews import complete_transfer_chain, CHILD
    from test_writing_stage_review_consumers import advance_clock
    from home_study_agent import learning_input_replan

    db, old_sid = writing_chain[:2]
    _, far, _ = complete_transfer_chain(writing_chain)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='completed',status='completed',ended_at=? WHERE session_id=?",
                  (far['attempt']['created_at'], old_sid))
        c.execute("UPDATE agent_objective SET status='completed',completed_at=? WHERE child_id=? AND status='active'",
                  (far['attempt']['created_at'], CHILD))
        c.commit()
    clock = advance_clock(monkeypatch, datetime.fromisoformat(far['attempt']['created_at']) + timedelta(days=8))
    monkeypatch.setattr(learning_input_replan, 'utc_now', lambda: clock.now(timezone.utc))
    repo = StudyRepository(db)
    sid = repo.start_session(CHILD, 'semantic-review-day')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1 WHERE session_id=?", (sid,))
        c.execute('''UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=120,
            task_segment_minutes=30,pet_min_minutes=15,competition_minutes=0 WHERE child_id=?''', (CHILD,))
        c.commit()
    workspace = repo.generate_plan(sid, 'semantic-review-plan')
    item = next(p for p in workspace['plan_items'] if p['source_type'] == 'due_review')
    repo.transition(sid, 'plan_review', 'study', 'Synthetic daily plan accepted', 'semantic-plan-accepted')
    gateway = Gateway({'utterance_binding': {'role': 'task_request', 'quote': '开始今天复查'},
        'objective': {'mode': 'new', 'type': 'execution', 'summary': '开始今天复查',
                      'success_criteria': ['原复查题已呈现'], 'phase': 'execute'},
        'next_step': {'capability': 'task.start', 'arguments': {}}, 'spoken_transition': '开始复查。',
        'requires_child_reply': False, 'after_execution': 'stop', 'confidence': .99})
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False)
    original_complete = gateway.complete
    async def complete_with_start_classification(request):
        result = await original_complete(request)
        if request.request_type == 'voice_interaction' and 'next_step' in gateway.output:
            from dataclasses import replace
            return replace(result, content=json.dumps({'role': 'control', 'control': 'task.start',
                'quote': '开始今天复查', 'confidence': .99}))
        return result
    monkeypatch.setattr(gateway, 'complete', complete_with_start_classification)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        started = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': '开始今天复查', 'client_action_id': 'semantic-start-review'})
        assert started.status_code == 200, started.text
        result = client.post(f"/v1/agent-turns/{started.json()['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        prompt = result.json()['execution']['result']['diagnostic_prompt']['prompt']
        quote = '这项先暂停，保留进度'
        gateway.output = {'role': 'control', 'control': 'task.pause', 'quote': quote, 'confidence': .99}
        voice = capture(client, settings, monkeypatch, sid, quote, prompt, 'semantic-pause-voice')
        paused = interpret(client, sid, quote, 'semantic-pause-task', voice)
        assert client.post(f"/v1/agent-turns/{paused['agent_turn_id']}/execute").status_code == 200
        # A newer paused review of the same subject must not steal the shown question.
        with db.connect() as c:
            c.execute('''INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,
                position,source_type,task_type) SELECT 'another-review',plan_id,'英语','PRIVATE SECOND REVIEW',5,1,
                'pending',99,'school_homework','review' FROM plan_item WHERE plan_item_id=?''', (item['plan_item_id'],))
            c.commit()
        repo.start_plan_item('another-review', 'another-review-start')
        repo.pause_plan_item('another-review', 'another-review-pause')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        quote = '刚才的复习我接着做'
        gateway.output = {'role': 'control', 'control': 'task.resume', 'quote': quote, 'confidence': .99,
            'selector': {'subject': None, 'task_kind': 'review', 'relative_order': 'displayed_question'}}
        voice = capture(client, settings, monkeypatch, sid, quote, prompt, 'semantic-review-voice')
        turn = interpret(client, sid, quote, 'semantic-review-resume', voice)
        assert turn['action']['type'] == 'resume_paused_task', turn
        result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        restored = result.json()['execution']['result']['diagnostic_prompt']['prompt']
        assert (restored['prompt_id'], restored['item_version']) == (prompt['prompt_id'], prompt['item_version'])
        assert repo._get_plan_item_by_id('another-review')['status'] == 'paused'
        assert repo._get_plan_item_by_id(item['plan_item_id'])['status'] == 'active'
        assert client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute").status_code == 200
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?', (sid,)).fetchone()[0] == 1
    for request in gateway.requests:
        if request.request_type == 'voice_interaction':
            text = json.dumps(request.messages, ensure_ascii=False)
            assert all(secret not in text for secret in (prompt['prompt_id'], prompt['item']['prompt'],
                                                        'another-review', 'PRIVATE SECOND REVIEW'))
            assert set(json.loads(request.messages[-1]['content'])) == {'transcript', 'state'}


@pytest.mark.parametrize('case', ['two_math', 'missing_subject', 'unbound_recent', 'negative', 'later', 'model_id'])
def test_selector_cannot_guess_or_override_negation(environment, monkeypatch, case):
    settings, db = environment
    gateway = SelectorGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, repo, prompt = two_paused(client, settings, db, monkeypatch, gateway, same_subject=case == 'two_math')
        quote = {'negative': '先别继续数学那项', 'later': '数学那项明天再做',
                 'unbound_recent': '刚才的复习'} .get(case, '数学那项我接着做')
        gateway.selector = {'subject': 'physics' if case == 'missing_subject' else 'math',
                            'task_kind': 'review' if case == 'unbound_recent' else None,
                            'relative_order': 'displayed_question' if case == 'unbound_recent' else None}
        if case == 'model_id':
            gateway.selector['plan_item_id'] = 'control-task-1'
        turn = hear(client, settings, monkeypatch, gateway, sid, prompt, quote, 'ambiguous-natural', 'task.resume')
        assert turn['action']['type'] == 'none', turn
        response = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert response.status_code == 200, response.text
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'paused'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 0


@pytest.mark.parametrize('mutation', ['plan_version', 'nonselected_subject', 'cancel_offer', 'other_child', 'cancel_voice', 'write_time_cancel'])
def test_natural_selection_rechecks_original_candidates_and_authority(environment, monkeypatch, mutation):
    from home_study_agent.repository import StudyRepository
    settings, db = environment
    gateway = SelectorGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, repo, prompt = two_paused(client, settings, db, monkeypatch, gateway)
        offer = hear(client, settings, monkeypatch, gateway, sid, prompt, '暂停的任务接着做', 'offered-original', 'task.resume')
        assert client.post(f"/v1/agent-turns/{offer['agent_turn_id']}/execute").status_code == 200
        gateway.selector = {'subject': 'math', 'task_kind': None, 'relative_order': None}
        selected = hear(client, settings, monkeypatch, gateway, sid, prompt, '先做数学的那项', 'selected-original', 'task.resume')
        assert selected['action']['type'] == 'resume_paused_task', selected
        if mutation == 'cancel_voice':
            assert client.post(f'/v1/sessions/{sid}/asr/actions/selected-original-voice/cancel').status_code == 200
        elif mutation == 'write_time_cancel':
            original = StudyRepository.resume_plan_item
            def cancelled_write(self, *args, **kwargs):
                with db.connect() as c:
                    c.execute("UPDATE voice_control_intent_request SET status='cancelled' WHERE session_id=?", (sid,))
                    c.commit()
                return original(self, *args, **kwargs)
            monkeypatch.setattr(StudyRepository, 'resume_plan_item', cancelled_write)
        else:
            with db.connect() as c:
                if mutation == 'plan_version':
                    c.execute("UPDATE daily_plan SET version=version+1 WHERE plan_id='control-plan'")
                elif mutation == 'nonselected_subject':
                    c.execute("UPDATE plan_item SET subject='数学' WHERE plan_item_id='control-task-2'")
                elif mutation == 'cancel_offer':
                    c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (offer['agent_turn_id'],))
                else:
                    c.execute("UPDATE daily_plan SET child_id=? WHERE plan_id='control-plan'", (OTHER,))
                c.commit()
        result = client.post(f"/v1/agent-turns/{selected['agent_turn_id']}/execute")
        assert result.status_code == 409, result.text
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('changed_during_model', [False, True])
def test_direct_natural_resume_checks_arrival_before_persist(environment, monkeypatch, changed_during_model):
    settings, db = environment
    gateway = SelectorGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, repo, prompt = two_paused(client, settings, db, monkeypatch, gateway)
        gateway.selector = {'subject': 'english', 'task_kind': None, 'relative_order': None}
        if changed_during_model:
            def change():
                with db.connect() as c:
                    c.execute("UPDATE daily_plan SET version=version+1 WHERE plan_id='control-plan'")
                    c.commit()
            gateway.hook = change
        turn = hear(client, settings, monkeypatch, gateway, sid, prompt, '先继续英语', 'fresh-natural-resume', 'task.resume')
        assert turn['action']['type'] == ('none' if changed_during_model else 'resume_paused_task'), turn
        response = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert response.status_code == 200, response.text
        assert repo._get_plan_item_by_id('control-task-2')['status'] == ('paused' if changed_during_model else 'active')
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'


@pytest.mark.parametrize('choice', ['数学那项', '第一项'])
def test_repeated_clarification_cannot_wash_cancelled_original_offer(environment, monkeypatch, choice):
    settings, db = environment
    gateway = SelectorGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, repo, prompt = two_paused(client, settings, db, monkeypatch, gateway)
        first = hear(client, settings, monkeypatch, gateway, sid, prompt, '暂停的任务继续', 'first-offer-lineage', 'task.resume')
        assert client.post(f"/v1/agent-turns/{first['agent_turn_id']}/execute").status_code == 200
        second = hear(client, settings, monkeypatch, gateway, sid, prompt, '我想接着做任务', 'second-offer-lineage', 'task.resume')
        assert second['action']['source'] == 'task_resume_clarification'
        assert client.post(f"/v1/agent-turns/{second['agent_turn_id']}/execute").status_code == 200
        with db.connect() as c:
            c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (first['agent_turn_id'],))
            c.commit()
        gateway.quote, gateway.control = choice, 'task.resume'
        gateway.selector = {'subject': 'math', 'task_kind': None, 'relative_order': None}
        voice = capture(client, settings, monkeypatch, sid, choice, prompt, 'lineage-choice-voice')
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': choice, 'client_action_id': 'lineage-choice', 'voice_input_id': voice})
        assert response.status_code in {200, 409}, response.text
        if response.status_code == 200:
            assert response.json()['action']['type'] == 'none'
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 0
