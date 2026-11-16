"""A feedback-guided retry is a new original answer, not a new unseen item."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_document_open_check import opened, answer_open, evaluation, snapshot
from test_document_open_followup import evaluated
from test_document_revisit import say


def retry(j, monkeypatch, key):
    result = say(j, monkeypatch, '我想按刚才的建议把同一道题重新解释一遍', 'retry_answer', key)
    check = result['execution']['result'].get('diagnostic_prompt')
    assert check and check['status'] == 'waiting_answer', result
    assert '反馈后' in result['execution']['response_text']
    return result, check['prompt']


def test_spoken_retry_new_original_reassessment_and_restart(tmp_path, monkeypatch):
    with opened(tmp_path, monkeypatch, 'spoken_explanation') as j:
        first, saved, follow = evaluated(j, monkeypatch, 'retry-first')
        before = j['answer']
        issued, second = retry(j, monkeypatch, 'retry-request')
        assert second['prompt_id'] != first['prompt_id']
        assert second['diagnostic_item_id'] == first['diagnostic_item_id']
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
            j['client'] = restarted
            restored = restarted.get(f'/v1/sessions/{j["sid"]}/diagnostic-prompt').json()
            assert restored['status'] == 'waiting_answer'
            assert restored['prompt']['prompt_id'] == second['prompt_id']
            replay = restarted.post('/v1/agent-turns/'+issued['turn']['agent_turn_id']+'/execute')
            assert replay.status_code == 200, replay.text
            assert replay.json()['execution']['replay_audio_allowed'] is False
            j['answer'] += '我还要用结果乘回原来的人数，检查总数是否相等。'
            saved2, body = answer_open(j, monkeypatch, second, 'spoken_explanation', 'retry-answer')
            assert saved2['local_learning_evaluation']['status'] == 'feedback_required', saved2
            j['responses'].append(evaluation(j, second))
            again = restarted.post('/v1/agent-turns/'+saved2['local_learning_evaluation']['feedback_turn_id']+'/execute')
            assert again.status_code == 200, again.text
            assert again.json()['execution']['result']['formative_only']
            status = say(j, monkeypatch, '我重新解释的这次怎么样', 'status', 'retry-status')
            assert '参考' in status['execution']['response_text']
            assert '暂不据此判断' not in status['execution']['response_text']
            old = restarted.get(f'/v1/sessions/{j["sid"]}/diagnostic-prompt', params={'prompt_id':first['prompt_id']})
            assert old.status_code == 200 and old.json()['attempt']['submitted_answer'] == before
            assert restarted.post(f'/v1/sessions/{j["sid"]}/diagnostic-prompt/answer', json=body).status_code == 200
            with j['db'].connect() as c:
                attempts = c.execute('''SELECT a.*,e.hint_level FROM diagnostic_attempt a
                    JOIN learning_evidence e USING(evidence_id) ORDER BY a.rowid''').fetchall()
                assert len(attempts) == 2
                assert attempts[0]['submitted_answer'] == before
                assert attempts[1]['submitted_answer'] == j['answer']
                assert attempts[1]['hint_level'] >= 1
                assert all(a['correctness'] is None for a in attempts)
                assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0] == 2
                req = json.loads(c.execute('SELECT request_json FROM diagnostic_prompt WHERE prompt_id=?', (second['prompt_id'],)).fetchone()[0])
                parent = req['document_independent']['binding']['spoken_retry']
                assert parent['prompt_id'] == first['prompt_id'] and parent['attempt_id'] == saved['attempt']['attempt_id']
                assert parent['after_feedback'] is True
                assert not c.execute('SELECT 1 FROM mastery_state WHERE evidence_count>0').fetchone()
            assert len(snapshot(j)['practice']['independent_results']) == 2
            # A second revision has the immediately previous original as parent;
            # it does not overwrite the first revision or flatten the lineage.
            issued3, third = retry(j, monkeypatch, 'retry-third-request')
            j['answer'] += '所以这个检查可以说明计算和原题条件一致。'
            saved3, _ = answer_open(j, monkeypatch, third, 'spoken_explanation', 'retry-third-answer')
            j['responses'].append(evaluation(j, third))
            result3 = j['client'].post('/v1/agent-turns/'+saved3['local_learning_evaluation']['feedback_turn_id']+'/execute')
            assert result3.status_code == 200, result3.text
            assert result3.json()['execution']['result']['formative_only']
            with j['db'].connect() as c:
                assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 3
                assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0] == 3
            assert len(snapshot(j)['practice']['independent_results']) == 3


@pytest.mark.parametrize('fault', ['source', 'answer', 'review'])
def test_retry_rechecks_parent_before_next_answer(tmp_path, monkeypatch, fault):
    with opened(tmp_path, monkeypatch, 'spoken_explanation') as j:
        first, saved, follow = evaluated(j, monkeypatch, 'changed-parent')
        issued, second = retry(j, monkeypatch, 'changed-retry')
        with j['db'].connect() as c:
            if fault == 'source':
                c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (first['diagnostic_item_id'],))
            elif fault == 'answer':
                c.execute("UPDATE diagnostic_attempt SET submitted_answer='changed parent' WHERE attempt_id=?", (saved['attempt']['attempt_id'],))
            else:
                c.execute("UPDATE model_call SET safety_status='blocked' WHERE purpose='learning_outcome_evaluation'")
            c.commit()
        calls = len(j['calls'])
        read = j['client'].get(f'/v1/sessions/{j["sid"]}/diagnostic-prompt', params={'prompt_id':second['prompt_id']})
        assert read.status_code in {200,409,422}, read.text
        if read.status_code == 200:
            assert read.json()['status'] != 'waiting_answer', read.text
        assert len(j['calls']) == calls
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 1


@pytest.mark.parametrize('fault', ['assistance', 'original_voice', 'cycle'])
def test_retry_reassessment_cannot_lose_assistance_or_original(tmp_path, monkeypatch, fault):
    with opened(tmp_path, monkeypatch, 'spoken_explanation') as j:
        first, _, _ = evaluated(j, monkeypatch, 'invalid-first')
        _, second = retry(j, monkeypatch, 'invalid-retry')
        j['answer'] += '再乘回原来的人数检查。'
        saved, _ = answer_open(j, monkeypatch, second, 'spoken_explanation', 'invalid-answer')
        with j['db'].connect() as c:
            if fault == 'assistance':
                c.execute('UPDATE learning_evidence SET hint_level=0 WHERE evidence_id=?', (saved['attempt']['evidence_id'],))
            elif fault == 'original_voice':
                c.execute("""UPDATE voice_input SET transcript='changed original' WHERE voice_input_id=
                    (SELECT json_extract(t.action_json,'$.source_voice_input_id') FROM agent_turn t
                    JOIN diagnostic_prompt p ON p.authorized_turn_id=t.agent_turn_id WHERE p.prompt_id=?)""", (second['prompt_id'],))
            else:
                c.execute("""UPDATE diagnostic_prompt SET request_json=json_set(request_json,
                    '$.document_independent.binding.spoken_retry.prompt_id',prompt_id) WHERE prompt_id=?""", (second['prompt_id'],))
            c.commit()
        calls = len(j['calls'])
        j['responses'].append(evaluation(j, second))
        result = j['client'].post('/v1/agent-turns/'+saved['local_learning_evaluation']['feedback_turn_id']+'/execute')
        assert result.status_code in {200,409,422}, result.text
        if result.status_code == 200:
            assert not result.json().get('execution',{}).get('result',{}).get('rubric_assessment'), result.text
        assert len(j['calls']) == calls
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 2
            assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0] == 1


@pytest.mark.parametrize('fault', ['changed_parent', 'transaction_failure'])
def test_retry_preparation_is_atomic_and_rechecks_after_classification(tmp_path, monkeypatch, fault):
    with opened(tmp_path, monkeypatch, 'spoken_explanation') as j:
        first, saved, _ = evaluated(j, monkeypatch, 'atomic-first')
        turn = say(j, monkeypatch, '让我自己重新解释这道题', 'retry_answer', 'atomic-retry', execute=False)
        with j['db'].connect() as c:
            prompts = [tuple(r) for r in c.execute('SELECT * FROM diagnostic_prompt ORDER BY rowid')]
            original_work = snapshot(j)
            if fault == 'changed_parent':
                c.execute("UPDATE diagnostic_attempt SET submitted_answer='changed parent' WHERE attempt_id=?", (saved['attempt']['attempt_id'],))
                c.commit()
        if fault == 'transaction_failure':
            import home_study_agent.document_open_check as module
            def fail(*args):
                raise ValueError('DOCUMENT_RETRY_SYNTHETIC_TRANSACTION_FAILURE')
            monkeypatch.setattr(module, 'bind_before_answer', fail)
        response = j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert response.status_code in {409,422}, response.text
        with j['db'].connect() as c:
            assert [tuple(r) for r in c.execute('SELECT * FROM diagnostic_prompt ORDER BY rowid')] == prompts
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 1
        assert snapshot(j) == original_work
