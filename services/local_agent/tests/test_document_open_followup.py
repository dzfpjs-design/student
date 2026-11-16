"""Spoken open answers must continue the original review without losing evidence."""
import json

import pytest

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_document_open_check import opened, answer_open, evaluation, snapshot
from test_document_revisit import say


def evaluated(j, monkeypatch, key):
    issued = say(j, monkeypatch, '换一道新题让我解释', 'independent_check', key)
    prompt = issued['execution']['result']['diagnostic_prompt']['prompt']
    saved, _ = answer_open(j, monkeypatch, prompt, 'spoken_explanation', key+'-answer')
    follow = saved['local_learning_evaluation']
    assert follow.get('feedback_turn_id'), follow
    j['responses'].append(evaluation(j, prompt))
    result = j['client'].post('/v1/agent-turns/'+follow['feedback_turn_id']+'/execute')
    assert result.status_code == 200, result.text
    assert result.json()['execution']['result']['formative_only']
    return prompt, saved, follow


def test_spoken_open_feedback_new_question_and_finish_after_restart(tmp_path, monkeypatch):
    with opened(tmp_path, monkeypatch, 'spoken_explanation') as j:
        prompt, saved, follow = evaluated(j, monkeypatch, 'spoken-first')
        with j['db'].connect() as c:
            criteria = c.execute('SELECT success_criteria_json FROM agent_objective WHERE objective_id=?',
                (snapshot(j)['practice']['objective_id'],)).fetchone()[0]
            reviews = [tuple(r) for r in c.execute('SELECT * FROM review_task ORDER BY review_task_id')]
        status = say(j, monkeypatch, '我刚才解释得怎么样', 'status', 'spoken-status')
        assert '参考' in status['execution']['response_text']
        assert '目前还不能可靠确认' not in status['execution']['response_text']
        helped = say(j, monkeypatch, '我想看看刚才那道题哪里需要改进', 'help', 'spoken-help')
        assert prompt['item']['prompt'] in helped['execution']['response_text']
        with j['db'].connect() as c:
            assert c.execute('SELECT success_criteria_json FROM agent_objective WHERE objective_id=?',
                (snapshot(j)['practice']['objective_id'],)).fetchone()[0] == criteria
        second, saved2, follow2 = evaluated(j, monkeypatch, 'spoken-second')
        assert second['diagnostic_item_id'] != prompt['diagnostic_item_id']
        assert saved2['attempt']['attempt_id'] != saved['attempt']['attempt_id']
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
            j['client'] = restarted
            done = say(j, monkeypatch, '这道题先回顾到这里', 'finish_check', 'spoken-finish')
            assert done['execution']['result']['status'] == 'check_closed'
            replay = restarted.post('/v1/agent-turns/'+done['turn']['agent_turn_id']+'/execute')
            assert replay.status_code == 200, replay.text
            assert replay.json()['execution']['result']['status'] == 'check_closed'
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 2
            assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0] == 2
            assert not c.execute('SELECT 1 FROM diagnostic_attempt WHERE correctness IS NOT NULL').fetchone()
            assert [tuple(r) for r in c.execute('SELECT * FROM review_task ORDER BY review_task_id')]==reviews


@pytest.mark.parametrize('fault', ['source', 'criteria', 'voice', 'model', 'execution', 'answer'])
def test_spoken_feedback_rechecks_current_authority(tmp_path, monkeypatch, fault):
    with opened(tmp_path, monkeypatch, 'spoken_explanation') as j:
        prompt, saved, follow = evaluated(j, monkeypatch, 'fault-first')
        # Establish the review focus before a source changes. The next voice
        # request must refuse stale advice, not lose the child's saved answer.
        say(j, monkeypatch, '我刚才解释得怎么样', 'status', 'fault-status')
        with j['db'].connect() as c:
            if fault == 'source':
                c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (prompt['diagnostic_item_id'],))
            elif fault == 'criteria':
                c.execute("UPDATE agent_objective SET success_criteria_json='[\"changed criterion\"]' WHERE objective_id=?",
                    (snapshot(j)['practice']['objective_id'],))
            elif fault == 'voice':
                c.execute("""UPDATE voice_input SET transcript='changed response' WHERE voice_input_id=
                    (SELECT json_extract(t.action_json,'$.source_voice_input_id') FROM agent_turn t
                    JOIN diagnostic_prompt p ON p.authorized_turn_id=t.agent_turn_id WHERE p.prompt_id=?)""",
                    (prompt['prompt_id'],))
            elif fault == 'model':
                c.execute("UPDATE model_call SET safety_status='blocked' WHERE purpose='learning_outcome_evaluation'")
            elif fault == 'execution':
                c.execute("""UPDATE agent_step SET arguments_json=json_set(arguments_json,'$.question_asked','changed question')
                    WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)""", (follow['feedback_turn_id'],))
            else:
                c.execute("UPDATE diagnostic_attempt SET submitted_answer='altered answer' WHERE attempt_id=?", (saved['attempt']['attempt_id'],))
            c.commit()
        calls = len(j['calls'])
        result = say(j, monkeypatch, '能解释刚才那道题吗', 'help', 'fault-help')
        assert '暂不据此判断对错' in result['execution']['response_text']
        assert prompt['item']['prompt'] not in result['execution']['response_text']
        assert len(j['calls']) == calls + 1  # Intent only, no new evaluation.
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0] == 1


@pytest.mark.parametrize('mode', ['written', 'spoken_explanation'])
def test_completed_evaluation_cannot_steal_another_turn_or_written_work(tmp_path, monkeypatch, mode):
    from home_study_agent.document_revisit import focus
    with opened(tmp_path, monkeypatch, mode) as j:
        issued = say(j, monkeypatch, '换道新题', 'independent_check', 'focus-first')
        prompt = issued['execution']['result']['diagnostic_prompt']['prompt']
        saved, _ = answer_open(j, monkeypatch, prompt, mode)
        follow = saved['local_learning_evaluation']
        j['responses'].append(evaluation(j, prompt))
        response = j['client'].post('/v1/agent-turns/'+follow['feedback_turn_id']+'/execute')
        assert response.status_code == 200, response.text
        with j['db'].connect() as c:
            c.execute('BEGIN')
            bound = focus(c, j['sid'], prompt['child_id'], 'probe')
            assert bool(bound) == (mode == 'spoken_explanation')
            # A later unrelated turn cannot be overridden by an older result.
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,orchestrator_version,created_at,client_action_id)
                SELECT 'other-turn',child_id,session_id,'new request','other','', '{}',orchestrator_version,
                created_at,'other-action' FROM agent_turn WHERE agent_turn_id=?''', (follow['feedback_turn_id'],))
            assert focus(c, j['sid'], prompt['child_id'], 'probe2') is None
            c.rollback()
