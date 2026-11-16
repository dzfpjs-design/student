"""Primary authored tasks across days; real HTTP/storage, synthetic voice/model."""
from datetime import datetime, timedelta, timezone
from contextlib import ExitStack
from synthetic_unclean_app_process import unclean_app
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.learning_state import LearningStateService
from home_study_agent import oral_daily_check
from test_open_response_spoken_runtime_http import test_natural_spoken_process_asr_bound_answer_formative_and_restart as run_journey
from test_voice_runtime_binding import capture, interpret
from test_writing_stage_review_consumers import advance_clock


@pytest.mark.parametrize('subject', ['数学', '语文', '英语'])
@pytest.mark.parametrize('restart_midway', [False, True])
def test_primary_answer_becomes_distinct_next_day_check(tmp_path, monkeypatch, subject, restart_midway):
    advance_clock(monkeypatch, datetime(2026, 9, 14, 8, tzinfo=timezone.utc))
    def after_answer(case):
        with ExitStack() as resumed_clients:
            client, db, sid, child = (case[k] for k in ('client', 'db', 'sid', 'child'))

            def post(path, body):
                response = client.post(path, json=body)
                assert response.status_code in (200, 201), response.text
                return response.json()

            feedback = post('/v1/agent-turns/' + case['follow']['feedback_turn_id'] + '/execute', {})
            assert feedback['execution']['result']['formative_only']
            post(f'/v1/sessions/{sid}/transition', dict(expected_state='study', target_state='reflection',
                reason='今天到这里，明天再检查。', client_action_id='primary-reflection'))
            post(f'/v1/sessions/{sid}/reflection/complete', dict(learned_text='', difficult_text='',
                client_action_id='primary-close'))
            summary = LearningStateService(db).close_session_learning(sid)
            evidence = case['attempt']['evidence_id']
            assert any(r['source']['source_id'] == evidence for r in summary['needs_verification'])
            with db.connect() as c:
                goal = c.execute('SELECT objective_id FROM agent_plan WHERE agent_turn_id=?',
                    (case['follow']['feedback_turn_id'],)).fetchone()[0]
            followups = [r for r in summary['remediation'] if r.get('objective_id') == goal]
            assert len(followups) == 1, summary['remediation']
            root = followups[0]['source_ref']
            assert followups[0]['mastery_update_allowed'] is False
            with db.connect() as c:
                ended = c.execute('SELECT ended_at FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0]
            next_day_clock = advance_clock(monkeypatch, datetime.fromisoformat(ended) + timedelta(days=2))
            # AgentRuntime writes the objective/answer ordering evidence directly.
            # Keep it on the same synthetic family day as the repository services.
            from home_study_agent import agent_runtime, scheduled_writing_goal
            monkeypatch.setattr(agent_runtime, 'datetime', next_day_clock)
            monkeypatch.setattr(scheduled_writing_goal, 'datetime', next_day_clock)
            client.headers['X-HSC-Parent'] = post('/v1/parent/unlock', {'pin': '8264'})['parent_token']
            rule = client.put(f'/v1/parent/children/{child}/family-rule', json=dict(
                latest_end_time='21:00', max_learning_minutes=90, task_segment_minutes=25, break_minutes=5,
                pet_min_minutes=15, competition_minutes=10, observation_mode='shadow', reminder_budget_per_10m=0))
            assert rule.status_code == 200, rule.text
            next_sid = post('/v1/sessions', dict(child_id=child, client_action_id='primary-next-day'))['session_id']
            with db.connect() as c:
                c.execute('BEGIN')
                proposed = oral_daily_check.planning(c, child, next_sid, root)
                assert proposed and proposed['status'] == 'ready', proposed
                assert oral_daily_check.original(c, 'child-junior-3', root) is None
            context = client.put(f'/v1/sessions/{next_sid}/planning-context', json=dict(
                energy_level='medium', fixed_commitment_minutes=0, client_action_id='primary-next-context'))
            assert context.status_code == 200, context.text
            # Session intake is outside this test; the child has declared no homework.
            with db.connect() as c:
                c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?", (next_sid,))
                c.commit()
            workspace = post(f'/v1/sessions/{next_sid}/plan/generate', dict(client_action_id='primary-next-plan'))
            tasks = [r for r in workspace['plan_items'] if r['source_ref'] == root]
            assert len(tasks) == 1, [(r['source_type'], r['source_ref']) for r in workspace['plan_items']]
            task = tasks[0]
            for other in workspace['plan_items']:
                if other['plan_item_id'] != task['plan_item_id']:
                    post('/v1/plan-items/' + other['plan_item_id'] + '/reschedule', dict(
                        reason='先做昨天约定的检查。', client_action_id='primary-defer-' + other['plan_item_id']))
            post(f'/v1/sessions/{next_sid}/transition', dict(expected_state='plan_review', target_state='study',
                reason='开始复查。', client_action_id='primary-check-study'))
            quote = f'开始今天安排的{subject}复习'
            subject_code = {'数学': 'math', '语文': 'chinese', '英语': 'english'}[subject]
            start_selector = {'subject': subject_code, 'task_kind': 'review',
                'source_type': None, 'target_quote': quote}
            case['command_state'].update(control='task.start', quote=quote,
                start_selector=start_selector)
            from test_cloud_first_agent import Planner
            original_complete = case['gateway'].complete

            async def complete(request):
                if request.request_type == 'agent_plan':
                    return await Planner(case['command_state']['control'],
                        {'start_selector': case['command_state']['start_selector']},
                        case['command_state']['quote']).complete(request)
                return await original_complete(request)

            monkeypatch.setattr(case['gateway'], 'complete', complete)
            voice = capture(client, case['settings'], monkeypatch, next_sid, quote,
                key='primary-check-voice', child=child)
            turn = interpret(client, next_sid, quote, 'primary-check-intent', voice)
            assert turn.get('runtime_capability') == 'task.start', turn
            delivered = post('/v1/agent-turns/' + turn['agent_turn_id'] + '/execute', {})
            prompt = delivered['execution']['result']['diagnostic_prompt']['prompt']
            assert prompt['diagnostic_item_id'] != case['prompt']['diagnostic_item_id']
            assert prompt['item']['prompt'] != case['prompt']['item']['prompt']
            assert prompt['item']['subject'] == subject
            if restart_midway:
                client = resumed_clients.enter_context(unclean_app(case, lane='primary_oral'))
                restored = client.get(f'/v1/sessions/{next_sid}/diagnostic-prompt', params={'prompt_id': prompt['prompt_id']})
                assert restored.status_code == 200, restored.text
                assert restored.json()['prompt']['prompt_id'] == prompt['prompt_id']
                replay = client.post('/v1/agent-turns/' + turn['agent_turn_id'] + '/execute')
                assert replay.status_code == 200, replay.text
                assert replay.json()['execution']['result']['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']
                resume = f'继续刚才那道{subject}题，我还没回答。'
                case['command_state'].update(control='task.resume', quote=resume,
                    selector={'subject': {'数学': 'math', '语文': 'chinese', '英语': 'english'}[subject],
                        'task_kind': 'oral', 'relative_order': None})
                voice = capture(client, case['settings'], monkeypatch, next_sid, resume, prompt,
                    key='primary-resume-voice', child=child)
                command = interpret(client, next_sid, resume, 'primary-resume-intent', voice)
                with db.connect() as c:
                    decisions = [tuple(r) for r in c.execute('SELECT decision_json FROM voice_control_intent_request ORDER BY rowid DESC LIMIT 2')]
                assert command.get('runtime_capability') == 'task.resume', (command.get('response_text'), decisions,
                    {k: task[k] for k in ('subject', 'task_type', 'source_type')})
                resumed = post('/v1/agent-turns/' + command['agent_turn_id'] + '/execute', {})
                assert resumed['execution']['result']['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']
            response = {
                '数学': '原来四盒一共四十八支，送了两组，每组九支，一共十八支，剩三十支。',
                '语文': '主要写他第一次没有折好，拆开重新对齐，第二次纸船浮起来了。没有说水盆很大。',
                '英语': 'Her brother has no cup, so she gives him one. She does not give him both cups.',
            }[subject]
            case['command_state'].update(control='answer', quote=response)
            voice = capture(client, case['settings'], monkeypatch, next_sid, response, prompt,
                key='primary-next-answer-voice', child=child)
            reply = interpret(client, next_sid, response, 'primary-next-answer-intent', voice)
            if subject == '英语' and reply['intent'] == 'pending_answer_clarification':
                # Role confirmation retains the saved source-overlapping original,
                # without sending the source text to a control classifier.
                assert response not in json.dumps(case['posts'][-1:], ensure_ascii=False)
                post('/v1/agent-turns/' + reply['agent_turn_id'] + '/execute', {})
                confirmation = '刚才那句就是我的答案'
                case['command_state'].update(control='answer', quote=confirmation)
                confirmed = capture(client, case['settings'], monkeypatch, next_sid, confirmation,
                    prompt, key='primary-next-confirmation-voice', child=child)
                reply = interpret(client, next_sid, confirmation, 'primary-next-confirmation-intent', confirmed)
            assert reply['action']['source_voice_input_id'] == voice
            assert reply['action']['type'] == 'submit_diagnostic_answer', reply
            saved = post(f'/v1/sessions/{next_sid}/diagnostic-prompt/answer', dict(prompt_id=prompt['prompt_id'],
                item_version=prompt['item_version'], answer=response, client_action_id='primary-next-answer',
                authorized_turn_id=reply['agent_turn_id']))
            follow = saved['local_learning_evaluation']
            assert follow['status'] == 'feedback_required', follow
            evaluated = post('/v1/agent-turns/' + follow['feedback_turn_id'] + '/execute', {})
            assert evaluated['execution']['result']['formative_only']
            assert case['evaluations'][-1]['question'] == prompt['item']['prompt']
            assert case['evaluations'][-1]['response'] == response
            assert saved['attempt']['correctness'] is None
            finish = '这项复习我回答完，也核对过反馈了，结束吧。'
            case['command_state'].update(control='task.complete', quote=finish)
            voice = capture(client, case['settings'], monkeypatch, next_sid, finish, prompt,
                key='primary-next-finish-voice', child=child)
            command = interpret(client, next_sid, finish, 'primary-next-finish-intent', voice)
            with db.connect() as c:
                decision = c.execute('SELECT decision_json FROM voice_control_intent_request ORDER BY rowid DESC LIMIT 1').fetchone()[0]
            assert command.get('runtime_capability') == 'task.complete', (command.get('response_text'), decision)
            completed = post('/v1/agent-turns/' + command['agent_turn_id'] + '/execute', {})
            assert completed['execution']['result']['status'] == 'completed'
            from home_study_agent.child_model import ChildModelService
            projected = ChildModelService(db).snapshot(child)['payload']['learning']['reliable_feedback']
            assert not projected['mastery_claim_allowed']
            assert all(n['retention_success_count'] == n['independent_correct'] == 0 for n in projected['nodes'])
            with unclean_app(case, lane='primary_oral') as restarted:
                restored = restarted.get(f'/v1/sessions/{next_sid}/diagnostic-prompt', params={'prompt_id': prompt['prompt_id']})
                assert restored.status_code == 200, restored.text
                assert restored.json()['attempt']['submitted_answer'] == response
                assert restored.json()['attempt']['attempt_id'] == saved['attempt']['attempt_id']
                workspace = restarted.get(f'/v1/sessions/{next_sid}').json()
                assert next(r for r in workspace['plan_items'] if r['plan_item_id'] == task['plan_item_id'])['status'] == 'completed'
                assert ChildModelService(case['db']).snapshot(child)['payload']['learning']['reliable_feedback'] == projected
                client = restarted
                post(f'/v1/sessions/{next_sid}/transition', dict(expected_state='study', target_state='reflection',
                    reason='今天复查结束。', client_action_id='primary-second-reflection'))
                post(f'/v1/sessions/{next_sid}/reflection/complete', dict(learned_text='', difficult_text='',
                    client_action_id='primary-second-close'))
                second_summary = LearningStateService(db).close_session_learning(next_sid)
                with db.connect() as c:
                    next_goal = c.execute('SELECT objective_id FROM agent_plan WHERE agent_turn_id=?',
                        (follow['feedback_turn_id'],)).fetchone()[0]
                    second_end = c.execute('SELECT ended_at FROM daily_session WHERE session_id=?', (next_sid,)).fetchone()[0]
                duties = [r for r in second_summary['remediation'] if r.get('objective_id') == next_goal]
                assert len(duties) == 1, second_summary['remediation']
                next_root = duties[0]['source_ref']
                assert next_root != root and duties[0]['mastery_update_allowed'] is False
                advance_clock(monkeypatch, datetime.fromisoformat(second_end) + timedelta(days=2))
                third_sid = post('/v1/sessions', dict(child_id=child, client_action_id='primary-third-day'))['session_id']
                with db.connect() as c:
                    c.execute('BEGIN')
                    next_check = oral_daily_check.planning(c, child, third_sid, next_root)
                    assert next_check and next_check['status'] == 'ready', next_check
                    next_source = next_check['binding']['source']['diagnostic_item_id']
                    assert next_source not in {case['prompt']['diagnostic_item_id'], prompt['diagnostic_item_id']}
                context = client.put(f'/v1/sessions/{third_sid}/planning-context', json=dict(
                    energy_level='medium', fixed_commitment_minutes=0, client_action_id='primary-third-context'))
                assert context.status_code == 200, context.text
                with db.connect() as c:
                    c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?", (third_sid,))
                    c.commit()
                third_plan = post(f'/v1/sessions/{third_sid}/plan/generate', dict(client_action_id='primary-third-plan'))
                next_tasks = [r for r in third_plan['plan_items'] if r['source_ref'] == next_root]
                assert len(next_tasks) == 1 and next_tasks[0]['status'] == 'pending', next_tasks
                assert next_tasks[0]['started_at'] is None
                with db.connect() as c:
                    assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?', (third_sid,)).fetchone()[0] == 0
                with unclean_app(case, lane='primary_oral') as again:
                    next_state = again.get(f'/v1/sessions/{third_sid}').json()['plan_items']
                    assert [r for r in next_state if r['source_ref'] == next_root] == next_tasks
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id=?', (child,)).fetchone()[0] == 2
                assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?', (root,)).fetchone()[0] == 'completed'
                assert not c.execute('SELECT 1 FROM diagnostic_attempt WHERE child_id=?', ('child-junior-3',)).fetchone()
                assert not c.execute('SELECT 1 FROM mastery_state WHERE child_id=? AND evidence_count>0', (child,)).fetchone()
            wire = json.dumps(case['posts'], ensure_ascii=False)
            assert root not in wire and 'oral_daily_binding' not in wire


    run_journey(tmp_path, monkeypatch, 'authored-p4:' + subject, None, False, None, after_saved=after_answer)
