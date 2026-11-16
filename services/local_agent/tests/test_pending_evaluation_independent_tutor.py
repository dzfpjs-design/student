"""Real isolated HTTP/SQLite; providers and audio are synthetic."""
import json
from contextlib import ExitStack
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app

from home_study_agent.model_gateway import ModelResult
from test_pending_evaluation_independent_dialogue import context, ask
from test_pending_evaluation_control import facts
from test_foreground_independent_dialogue import foreground_models


QUESTION = '数学里两个负数相乘为什么得到正数？'


def tutor_models(j, monkeypatch, *, after_submission=None, narration=None):
    foreground_models(j, monkeypatch, QUESTION, capability='learning.explain',
                      arguments={'question': QUESTION, 'subject': '数学'})
    original = j['gateway'].complete
    async def complete(request):
        if not request.request_type.startswith('tutor_hint_level_'):
            return await original(request)
        request.pre_dispatch()
        j['gateway'].requests.append(request)
        if after_submission:
            after_submission()
        return ModelResult(content=json.dumps({'blackboard': {'title': '负数乘法', 'segments': [{
            'narration': narration or '保持分配律一致时，两个负数相乘就得到正数。',
            'blocks': [{'kind': 'text', 'text': '负数乘负数得到正数。'}],
        }]}}, ensure_ascii=False), provider='test', model='synthetic-tutor', latency_ms=1,
            prompt_tokens=20, completion_tokens=20)
    monkeypatch.setattr(j['gateway'], 'complete', complete)


def lesson_body(turn):
    action = turn['action']
    return {'question': action['question'], 'subject': action['subject'], 'hint_level': action['hint_level'],
            'client_action_id': action['tutor_request_action_id']}


def tutor_calls(j):
    return [request for request in j['gateway'].requests if request.request_type.startswith('tutor_hint_level_')]


def test_independent_learning_question_opens_tutor_without_changing_original_evaluation(tmp_path, monkeypatch):
    with context(tmp_path) as j:
        tutor_models(j, monkeypatch)
        before = facts(j['db'])
        planned, _ = ask(j, QUESTION)
        assert planned.status_code == 200, planned.text
        turn = planned.json()
        assert turn['action']['type'] == 'ask_tutor', turn['action']
        body = lesson_body(turn)
        lesson = j['client'].post(f"/v1/sessions/{j['sid']}/tutor", json=body)
        assert lesson.status_code == 200, lesson.text
        assert lesson.json()['question'] == QUESTION
        assert '分配律' in lesson.json()['answer']
        j['finish_client']()
        with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
            replay = restarted.post(f"/v1/sessions/{j['sid']}/tutor", json=body)
            assert replay.status_code == 200, replay.text
            assert replay.json()['turn_id'] == lesson.json()['turn_id']
        assert len(tutor_calls(j)) == 1
        for request in j['gateway'].requests:
            exported = json.dumps({'messages': request.messages, 'manifest': request.context_manifest,
                                   'refs': request.evidence_refs}, ensure_ascii=False)
            for private in ['child-runtime', j['sid'], '正弦', 'eval-action', '原作答私密证据']:
                assert private not in exported
        assert facts(j['db']) == before


@pytest.mark.parametrize('fault', ['cancel_original', 'change_answer', 'remove_voice', 'change_question', 'revoke_consent'])
@pytest.mark.parametrize('timing', ['before_submission', 'after_submission'])
def test_independent_tutor_rechecks_source_and_consent_without_saving_late_result(tmp_path, monkeypatch, fault, timing):
    with context(tmp_path) as j:
        def revoke():
            if fault == 'revoke_consent':
                j['privacy'].update_preferences({'cloud_model_text_allowed': False})
                return
            with j['db'].connect() as c:
                if fault == 'cancel_original':
                    c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id='eval'")
                elif fault == 'change_answer':
                    c.execute("UPDATE agent_step SET arguments_json=json_set(arguments_json,'$.response','replacement') WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id='eval')")
                elif fault == 'remove_voice':
                    c.execute("UPDATE agent_turn SET action_json=json_remove(action_json,'$.voice_input_id') WHERE agent_turn_id=?",
                              (turn['agent_turn_id'],))
                else:
                    c.execute(
                        "UPDATE agent_turn SET action_json=json_set(action_json,'$.question','different') WHERE agent_turn_id=?",
                        (turn['agent_turn_id'],))
                c.commit()
        tutor_models(j, monkeypatch, after_submission=revoke if timing == 'after_submission' else None)
        planned, _ = ask(j, QUESTION)
        assert planned.status_code == 200, planned.text
        turn = planned.json()
        assert turn['action']['type'] == 'ask_tutor', turn['action']
        if timing == 'before_submission':
            revoke()
        before = facts(j['db'])
        lesson = j['client'].post(f"/v1/sessions/{j['sid']}/tutor", json=lesson_body(turn))
        assert lesson.status_code in {403, 409, 422}, lesson.text
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM tutor_turn').fetchone()[0] == 0
        assert len(tutor_calls(j)) == int(timing == 'after_submission')
        assert facts(j['db']) == before


def test_independent_tutor_cannot_ask_a_second_check_question(tmp_path, monkeypatch):
    with context(tmp_path) as j:
        tutor_models(j, monkeypatch, narration='请你回答负三乘负二等于多少？')
        before = facts(j['db'])
        planned, _ = ask(j, QUESTION)
        assert planned.status_code == 200, planned.text
        lesson = j['client'].post(f"/v1/sessions/{j['sid']}/tutor", json=lesson_body(planned.json()))
        assert lesson.status_code == 409, lesson.text
        assert lesson.json()['code'] == 'FOREGROUND_TUTOR_REPLY_SCOPE_CHANGED'
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM tutor_turn').fetchone()[0] == 0
        assert facts(j['db']) == before


def test_resource_question_cannot_use_tutor_to_invent_resource_facts(tmp_path, monkeypatch):
    with context(tmp_path) as j:
        quote = '等评价的时候，你能告诉我有没有PET学习资料吗？'
        foreground_models(j, monkeypatch, quote, capability='learning.explain',
                          arguments={'question': quote, 'subject': '英语'})
        before = facts(j['db'])
        planned, _ = ask(j, quote)
        assert planned.status_code == 200, planned.text
        assert planned.json()['action']['type'] == 'inspect_learning_system', planned.json()['action']
        executed = j['client'].post(f"/v1/agent-turns/{planned.json()['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
        assert not tutor_calls(j) and facts(j['db']) == before


@pytest.mark.parametrize('timing', ['before_planning', 'before_submission', 'after_submission'])
def test_active_exam_cannot_be_bypassed_by_pending_evaluation_tutor(tmp_path, monkeypatch, timing):
    with context(tmp_path) as j:
        started = []
        def start_exam():
            service = j['client'].app.state.exam_simulation
            run = service.start(j['sid'], 'junior_exam', '数学', 'practice', 2, 30, 'pending-tutor-exam')
            assert run['status'] == 'active'
            with j['db'].connect() as c:
                started.append(tuple(c.execute('SELECT * FROM specialized_exam_run WHERE run_id=?',
                                               (run['run_id'],)).fetchone()))
        tutor_models(j, monkeypatch, after_submission=start_exam if timing == 'after_submission' else None)
        if timing == 'before_planning':
            start_exam()
        planned, _ = ask(j, QUESTION)
        assert planned.status_code == 200, planned.text
        if timing == 'before_planning':
            assert planned.json()['action']['type'] == 'none'
            assert not tutor_calls(j)
            plan_request = next(r for r in j['gateway'].requests if r.request_type == 'agent_plan')
            catalog = json.loads(plan_request.messages[1]['content'])['available_capabilities']
            assert 'learning.explain' not in {entry['name'] for entry in catalog}
        else:
            assert planned.json()['action']['type'] == 'ask_tutor'
            if timing == 'before_submission':
                start_exam()
            lesson = j['client'].post(f"/v1/sessions/{j['sid']}/tutor", json=lesson_body(planned.json()))
            assert lesson.status_code in {409, 422}, lesson.text
            assert len(tutor_calls(j)) == int(timing == 'after_submission')
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM tutor_turn').fetchone()[0] == 0
            assert tuple(c.execute('SELECT * FROM specialized_exam_run').fetchone()) == started[0]


def test_unknown_tutor_result_remains_read_only_across_restart(tmp_path, monkeypatch):
    from home_study_agent.model_gateway import ModelResponseError
    with context(tmp_path) as j:
        tutor_models(j, monkeypatch)
        original = j['gateway'].complete
        async def uncertain(request):
            if not request.request_type.startswith('tutor_hint_level_'):
                return await original(request)
            request.pre_dispatch()
            j['gateway'].requests.append(request)
            raise ModelResponseError('SYNTHETIC_CONNECTION_LOST')
        monkeypatch.setattr(j['gateway'], 'complete', uncertain)
        planned, _ = ask(j, QUESTION)
        assert planned.status_code == 200, planned.text
        body = lesson_body(planned.json())
        before = facts(j['db'])
        first = j['client'].post(f"/v1/sessions/{j['sid']}/tutor", json=body)
        assert first.status_code == 409 and first.json()['code'] == 'TUTOR_RESULT_UNCONFIRMED', first.text
        j['finish_client']()
        with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
            for _ in range(2):
                replay = restarted.post(f"/v1/sessions/{j['sid']}/tutor", json=body)
                assert replay.status_code == 409 and replay.json()['code'] == 'TUTOR_RESULT_UNCONFIRMED', replay.text
        assert len(tutor_calls(j)) == 1 and facts(j['db']) == before
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM tutor_turn').fetchone()[0] == 0
            assert tuple(c.execute('SELECT status,attempts,last_error_code FROM tutor_request_reservation WHERE action_id=?',
                                  (body['client_action_id'],)).fetchone()) == ('interrupted', 1, 'INTERRUPTED_UNKNOWN_OUTCOME')


def test_original_evaluation_completion_does_not_invalidate_independent_lesson(tmp_path, monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    from test_learning_outcome_evaluator import evaluation_fixture
    from test_learning_runtime_http import NoCloud
    from test_pending_evaluation_independent_dialogue import inline_settings
    db, sid, evaluator, evaluation_gateway, _ = evaluation_fixture(tmp_path)
    settings = inline_settings(tmp_path, db)
    with ExitStack() as original_client_lifecycle:
        client = original_client_lifecycle.enter_context(TestClient(create_app(settings, model_gateway=NoCloud())))
        privacy = PrivacyLifecycleService(db, tmp_path)
        privacy.initialize()
        privacy.update_preferences({'cloud_model_text_allowed': True})
        gateway = client.app.state.model_gateway
        gateway.requests = []
        j = {'settings': settings, 'db': db, 'sid': sid, 'client': client, 'gateway': gateway, 'privacy': privacy}
        tutor_models(j, monkeypatch)
        entered, released = Event(), Event()
        original = evaluation_gateway.complete
        async def held(request):
            entered.set()
            for _ in range(2000):
                if released.is_set():
                    return await original(request)
                await asyncio.sleep(.01)
            raise AssertionError('original evaluation release deadline')
        evaluation_gateway.complete = held
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(asyncio.run, evaluator.evaluate('eval'))
            try:
                assert entered.wait(10)
                planned, turn_body = ask(j, QUESTION)
                assert planned.status_code == 200, planned.text
            finally:
                released.set()
            result = running.result(timeout=15)
        assert result['evaluation']['certification_status'] == 'candidate'
        before = facts(db)
        body = lesson_body(planned.json())
        lesson = client.post(f'/v1/sessions/{sid}/tutor', json=body)
        assert lesson.status_code == 200, lesson.text
        original_client_lifecycle.close()
        with TestClient(create_app(settings, model_gateway=gateway)) as restarted:
            turn = restarted.post(f'/v1/sessions/{sid}/agent-turn', json=turn_body)
            assert turn.status_code == 200 and turn.json()['agent_turn_id'] == planned.json()['agent_turn_id'], turn.text
            replay = restarted.post(f'/v1/sessions/{sid}/tutor', json=body)
            assert replay.status_code == 200 and replay.json()['turn_id'] == lesson.json()['turn_id'], replay.text
        assert facts(db) == before and len(tutor_calls(j)) == 1 and len(evaluation_gateway.requests) == 1
