"""Confirmed original -> qualified distinct prompt -> existing answer pipeline."""
from contextlib import contextmanager
import json

import pytest

from home_study_agent.learning_state import ITEMS
from home_study_agent.app import create_app
from fastapi.testclient import TestClient
from test_diagnostic_prompt_http import environment, ITEM, SECOND, CHILD
import test_document_goal_http as original
from test_document_revisit import next_day, offer, say
from test_voice_runtime_binding import capture, interpret, submit


@contextmanager
def mapped(environment, monkeypatch):
    body = next(row[2] for row in ITEMS if row[0] == ITEM)
    monkeypatch.setattr(original, 'BODY', body)
    generator = original.journey.__wrapped__(environment, monkeypatch)
    j = next(generator)
    try:
        j['responses'].append('先看括号和运算顺序，请自己解释每一步。')
        next_day(j, monkeypatch)
        offer(j)
        say(j, monkeypatch, '我想先看看原题', 'start', 'original-start')
        yield j
    finally:
        generator.close()


@pytest.mark.parametrize('answer_text,correctness', [('42', 1), ('43', 0)])
def test_spoken_independent_request_opens_distinct_qualified_prompt(environment, monkeypatch, answer_text, correctness):
    with mapped(environment, monkeypatch) as j:
        result = say(j, monkeypatch, '换一道同样知识点的题考考我', 'independent_check', 'new-check')
        check = result['execution']['result']['diagnostic_prompt']
        assert check['status'] == 'waiting_answer'
        prompt = check['prompt']
        assert prompt['diagnostic_item_id'] == SECOND
        assert prompt['child_id'] == CHILD and prompt['origin_session_id'] == j['sid']
        assert prompt['item']['prompt'] in result['execution']['response_text']
        assert 'expected_answer' not in json.dumps(check)
        assert original.BODY not in json.dumps(j['calls'][-1], ensure_ascii=False)
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM mistake_case').fetchone()[0] == 0
        transcript = '我的答案是' + answer_text
        voice = capture(j['client'], j['settings'], monkeypatch, j['sid'], transcript, prompt, 'new-check-answer-voice')
        heard = interpret(j['client'], j['sid'], transcript, 'new-check-answer', voice)
        answer = submit(j['client'], j['sid'], prompt, heard, answer=answer_text)
        assert answer.status_code == 200, answer.text
        assert answer.json()['status'] == 'answered'
        assert answer.json()['attempt']['score_status'] == 'verified_local'
        assert answer.json()['attempt']['correctness'] == correctness
        assert answer.json()['attempt']['learning_adaptation']['status'] == 'refreshed'
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
            again = restarted.post('/v1/agent-turns/' + result['turn']['agent_turn_id'] + '/execute')
            assert again.status_code == 200, again.text
            assert again.json()['execution']['result']['status'] == 'independent_check_recorded'
            assert not again.json()['execution']['result'].get('diagnostic_prompt')
            row = restarted.get(f'/v1/sessions/{j["sid"]}/diagnostic-prompt').json()
            assert row['prompt']['prompt_id'] == prompt['prompt_id'] and row['status'] == 'answered'
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 1
            assert c.execute('SELECT independent FROM learning_evidence').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM mistake_case').fetchone()[0] == 1 - correctness
            reviews = c.execute('SELECT * FROM review_task WHERE source_evidence_id=?',
                (answer.json()['attempt']['evidence_id'],)).fetchall()
            assert {'48h', '7d'}.issubset({r['interval_label'] for r in reviews})
            assert all(r['child_id'] == CHILD and r['status'] ==
                ('pending' if correctness else 'cancelled') for r in reviews)


@pytest.mark.parametrize('change', ['original', 'new_item', 'mapping', 'binding', 'request_voice', 'deletion', 'cancelled'])
def test_changed_sources_cannot_score_or_retarget_the_prepared_question(environment, monkeypatch, change):
    with mapped(environment, monkeypatch) as j:
        result = say(j, monkeypatch, '请换一道新题让我独立做', 'independent_check', 'new-check')
        prompt = result['execution']['result']['diagnostic_prompt']['prompt']
        with j['db'].connect() as c:
            if change == 'original':
                c.execute("UPDATE capture_artifact SET extracted_text='changed original' WHERE artifact_id=?", (j['source']['artifact_id'],))
            elif change in {'mapping', 'new_item'}:
                c.execute("UPDATE diagnostic_item SET prompt='changed source' WHERE diagnostic_item_id=?", (ITEM if change == 'mapping' else SECOND,))
            elif change == 'binding':
                c.execute("UPDATE diagnostic_prompt SET request_json=json_remove(request_json,'$.document_independent') WHERE prompt_id=?", (prompt['prompt_id'],))
            elif change == 'request_voice':
                c.execute("UPDATE voice_input SET transcript='changed request' WHERE voice_input_id=?",
                    (result['turn']['action']['voice_input_id'],))
            elif change == 'deletion':
                c.execute("""INSERT INTO deletion_job (deletion_id,child_id,scope,status,created_at,updated_at)
                    VALUES ('delete-original',?,'tutor','pending','now','now')""", (CHILD,))
            else:
                c.execute("UPDATE agent_commitment SET status='cancelled' WHERE commitment_id=?",
                    (result['execution']['result']['commitment_id'],))
            c.commit()
        response = j['client'].post(f'/v1/sessions/{j["sid"]}/diagnostic-prompt/answer', json={
            'prompt_id':prompt['prompt_id'], 'item_version':prompt['item_version'], 'answer':'42', 'client_action_id':'invalidated-answer'})
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'invalidated'
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0] == 0


def test_source_changes_between_interpretation_and_execution_do_not_create_a_prompt(environment, monkeypatch):
    with mapped(environment, monkeypatch) as j:
        turn = say(j,monkeypatch,'换道新题考我','independent_check','new-check',execute=False)
        with j['db'].connect() as c:
            c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?",(SECOND,))
            c.commit()
        response=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert response.status_code == 409, response.text
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0] == 0


@pytest.mark.parametrize('missing', ['mapping', 'new_item', 'already_shown'])
def test_unavailable_source_keeps_request_without_inventing_a_question(environment, monkeypatch, missing):
    from test_diagnostic_prompt_http import prepare, binding
    from home_study_agent import document_check_supply as supply
    from home_study_agent.local_supply import LocalSupplyError
    def unsupported(*args):
        raise LocalSupplyError('LOCAL_TEMPLATE_UNSUPPORTED')
    monkeypatch.setattr(supply, 'build_candidate', unsupported)
    with mapped(environment, monkeypatch) as j:
        if missing == 'already_shown':
            shown = prepare(j['client'], j['sid'], preferred_item_id=SECOND)['prompt']
            cancelled = j['client'].post(f'/v1/sessions/{j["sid"]}/diagnostic-prompt/cancel', json=binding(shown))
            assert cancelled.status_code == 200, cancelled.text
        else:
            with j['db'].connect() as c:
                c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (ITEM if missing == 'mapping' else SECOND,))
                c.commit()
        result = say(j, monkeypatch, '换一道新题考我', 'independent_check', 'unavailable-check')
        assert not result['execution']['result'].get('diagnostic_prompt')
        with j['db'].connect() as c:
            work = json.loads(c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',
                (result['execution']['result']['commitment_id'],)).fetchone()[0])['practice']
            assert work['independent_request']['binding']['status'] == ('mapping_required' if missing == 'mapping' else 'new_material_required')
            assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0] == (1 if missing == 'already_shown' else 0)
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM mistake_case').fetchone()[0] == 0


def test_prepare_and_journal_roll_back_together_and_retry_uses_one_prompt(environment, monkeypatch):
    from home_study_agent import document_independent_check as check
    from home_study_agent.document_goal_handoff import DocumentGoalError
    with mapped(environment, monkeypatch) as j:
        turn = say(j, monkeypatch, '换一道新题考我', 'independent_check', 'atomic-check', execute=False)
        original_begin = check.begin
        def interrupted(*args):
            original_begin(*args)
            raise DocumentGoalError('INJECTED_AFTER_PREPARE')
        monkeypatch.setattr(check, 'begin', interrupted)
        url = '/v1/agent-turns/' + turn['agent_turn_id'] + '/execute'
        assert j['client'].post(url).status_code == 409
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0] == 0
            assert c.execute('SELECT execution_receipt_json FROM voice_control_intent_request WHERE request_key=?',
                (turn['action']['document_revisit_request'],)).fetchone()[0] is None
        monkeypatch.setattr(check, 'begin', original_begin)
        retried = j['client'].post(url)
        assert retried.status_code == 200, retried.text
        assert retried.json()['execution']['result']['diagnostic_prompt']['status'] == 'waiting_answer'
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0] == 1


def test_committed_prompt_survives_lost_step_receipt_without_replaying_question(environment, monkeypatch):
    from home_study_agent.agent_capability_executor import AgentCapabilityExecutor, AgentExecutionConflict
    with mapped(environment, monkeypatch) as j:
        turn = say(j, monkeypatch, '换道新题让我试试', 'independent_check', 'lost-step-check', execute=False)
        dispatch = AgentCapabilityExecutor._dispatch
        def interrupted(self, step):
            dispatch(self, step)
            raise AgentExecutionConflict('INJECTED_AFTER_COMMIT')
        monkeypatch.setattr(AgentCapabilityExecutor, '_dispatch', interrupted)
        url = '/v1/agent-turns/' + turn['agent_turn_id'] + '/execute'
        assert j['client'].post(url).status_code == 409
        monkeypatch.setattr(AgentCapabilityExecutor, '_dispatch', dispatch)
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
            current = restarted.get(f'/v1/sessions/{j["sid"]}/diagnostic-prompt')
            assert current.status_code == 200, current.text
            assert current.json()['status'] == 'waiting_answer'
            again = restarted.post(url)
            assert again.status_code == 200, again.text
            assert again.json()['execution']['result']['status'] == 'independent_check_recorded'
            assert not again.json()['execution']['result'].get('diagnostic_prompt')
            assert restarted.get(f'/v1/sessions/{j["sid"]}/diagnostic-prompt').json()['status'] == 'waiting_answer'
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0


def test_understanding_acknowledgement_neither_teaches_nor_scores(environment, monkeypatch):
    with mapped(environment, monkeypatch) as j:
        with j['db'].connect() as c:
            teachers = c.execute('SELECT count(*) FROM tutor_turn').fetchone()[0]
        result = say(j, monkeypatch, '我听懂了', 'unclear', 'understanding')
        assert result['execution']['result']['status'] == 'waiting_answer'
        assert not result['execution']['result'].get('diagnostic_prompt')
        assert '独立作答' in result['execution']['response_text']
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM tutor_turn').fetchone()[0] == teachers
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0] == 0
