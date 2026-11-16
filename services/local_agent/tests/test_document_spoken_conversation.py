"""The child can revisit a multi-utterance explanation without losing clauses."""
import json
import pytest

from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from test_document_open_check import opened, evaluation, answer_open
from test_document_open_followup import evaluated
from test_document_revisit import say
from test_document_spoken_retry import retry


def supplement(j, monkeypatch, parent, text, key):
    j['answer'] = text
    turn = say(j, monkeypatch, text, 'answer', key, execute=False)
    j['responses'].append(evaluation(j, parent))
    result = j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert result.status_code == 200, result.text
    value = result.json()['execution']
    assert value['result']['supplement_evaluation_status'] == 'completed', value
    return value['result']['diagnostic_prompt']['prompt']


def test_consecutive_speech_readback_restart_and_cloud_boundary(tmp_path, monkeypatch):
    with opened(tmp_path, monkeypatch, 'spoken_explanation') as j:
        parent, _, _ = evaluated(j, monkeypatch, 'chain-first')
        first = j['answer']
        second = '我还要把总数除以人数，得到平均每人的数量。'
        last = '不对，我改一下刚才的说法，最后还要乘回人数检查总数。'
        middle = supplement(j, monkeypatch, parent, second, 'chain-second')
        final = supplement(j, monkeypatch, middle, last, 'chain-third')
        count = len(j['calls'])
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
            j['client'] = restarted
            status = say(j, monkeypatch, '我前面几段解释合起来怎么样', 'status', 'chain-status')
            response = status['execution']['response_text']
            assert all(text in response for text in (first, second, last)), response
            chain = status['execution']['result']['spoken_conversation']
            assert [s['transcript'] for s in chain['segments']] == [first, second, last]
            assert [s['relation'] for s in chain['segments']] == ['initial','continuation','continuation']
            assert chain['active_prompt_ids'] == [parent['prompt_id'],middle['prompt_id'],final['prompt_id']]
            assert chain['combined_assessment_status'] == 'not_evaluated'
            assert chain['mastery_update_allowed'] is False and chain['cloud_transmission_allowed'] is False
            assert '不是整段解释的总评' in response
            replay = restarted.post('/v1/agent-turns/'+status['turn']['agent_turn_id']+'/execute')
            assert replay.status_code == 200, replay.text
            assert replay.json()['execution']['replay_audio_allowed'] is False
            assert len(j['calls']) == count+1  # Classification only, local readback.
            # Already-spoken originals are not added to the latest cloud payload.
            for call in j['calls'][count:]:
                assert all(text not in json.dumps(call, ensure_ascii=False) for text in (first,second,last))
            with j['db'].connect() as c:
                assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 3
                assert not c.execute('SELECT 1 FROM diagnostic_attempt WHERE correctness IS NOT NULL').fetchone()
                # Source withdrawal invalidates a previously cached readback as well.
                c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (parent['diagnostic_item_id'],))
                c.commit()
            rejected = j['client'].post('/v1/agent-turns/'+status['turn']['agent_turn_id']+'/execute')
            assert rejected.status_code in {409,422}, rejected.text
            assert first not in rejected.text and last not in rejected.text


def test_explicit_full_retry_starts_new_explanation_without_erasing_history(tmp_path, monkeypatch):
    with opened(tmp_path, monkeypatch, 'spoken_explanation') as j:
        parent, _, _ = evaluated(j, monkeypatch, 'reset-first')
        first = j['answer']
        middle = supplement(j, monkeypatch, parent, '我还要把数量乘回去验证。', 'reset-supplement')
        _, new = retry(j, monkeypatch, 'reset-request')
        j['answer'] = '我重新完整解释，先把两类数量相加，再除以人数，最后反过来验算。'
        saved, _ = answer_open(j, monkeypatch, new, 'spoken_explanation', 'reset-full-answer')
        j['responses'].append(evaluation(j,new))
        done = j['client'].post('/v1/agent-turns/'+saved['local_learning_evaluation']['feedback_turn_id']+'/execute')
        assert done.status_code == 200, done.text
        status = say(j, monkeypatch, '这一次完整重讲的进展呢', 'status', 'reset-status')
        chain = status['execution']['result']['spoken_conversation']
        assert [s['relation'] for s in chain['segments']] == ['initial','continuation','restart']
        assert chain['active_prompt_ids'] == [new['prompt_id']]
        assert chain['segments'][0]['transcript'] == first
        assert chain['segments'][1]['prompt_id'] == middle['prompt_id']
        assert first not in status['execution']['response_text']
        assert j['answer'] in status['execution']['response_text']
        from home_study_agent.document_spoken_conversation import read
        from home_study_agent.document_revisit import retained
        with j['db'].connect() as c:
            c.execute('BEGIN')
            statements = []
            c.set_trace_callback(statements.append)
            repeated = read(c, retained(c,j['cid'],new['child_id']))
            c.set_trace_callback(None)
            assert repeated == chain
            assert len(statements) < 3000, len(statements)  # Previously 5478 on this three-segment chain.
            # A prior successful read cannot survive a subsequent write.
            c.execute("UPDATE model_call SET safety_status='blocked' WHERE call_id=(SELECT model_call_id FROM rubric_assessment WHERE assessment_id=?)", (chain['segments'][0]['assessment_id'],))
            with pytest.raises(ValueError):
                read(c,retained(c,j['cid'],new['child_id']))
            c.rollback()


def test_feedback_read_scope_never_reuses_across_write_or_connection(tmp_path):
    import sqlite3
    from home_study_agent.document_open_feedback import read_scope
    with sqlite3.connect(':memory:') as first, sqlite3.connect(':memory:') as second:
        first.execute('CREATE TABLE source (value TEXT)')
        first.execute('BEGIN')
        second.execute('BEGIN')
        with pytest.raises(ValueError, match='DOCUMENT_FEEDBACK_READ_MUTATED'):
            with read_scope(first) as outer:
                with read_scope(first) as nested:
                    assert nested is outer
                with read_scope(second) as other:
                    assert other is not outer
                first.execute("INSERT INTO source VALUES ('changed')")
                with read_scope(first):
                    pytest.fail('must not reuse a mutated snapshot')
        with read_scope(first) as fresh:
            assert fresh is not outer and not fresh['values']


@pytest.mark.parametrize('fault', ['voice','rubric','cached_text','cached_chain'])
def test_conversation_replay_rechecks_prior_evidence_and_receipt(tmp_path, monkeypatch, fault):
    with opened(tmp_path, monkeypatch, 'spoken_explanation') as j:
        parent, _, _ = evaluated(j, monkeypatch, 'adversarial-first')
        first = j['answer']
        supplement(j, monkeypatch, parent, '我再用乘法检验一次数量关系。', 'adversarial-second')
        status = say(j, monkeypatch, '刚才前后两段讲得怎么样', 'status', 'adversarial-status')
        turn_id = status['turn']['agent_turn_id']
        chain = status['execution']['result']['spoken_conversation']
        with j['db'].connect() as c:
            if fault == 'voice':
                c.execute("UPDATE voice_input SET transcript='altered original' WHERE voice_input_id=(SELECT json_extract(action_json,'$.source_voice_input_id') FROM agent_turn WHERE agent_turn_id=(SELECT authorized_turn_id FROM diagnostic_prompt WHERE prompt_id=?))", (parent['prompt_id'],))
            elif fault == 'rubric':
                c.execute("UPDATE model_call SET safety_status='blocked' WHERE call_id=(SELECT model_call_id FROM rubric_assessment WHERE assessment_id=?)", (chain['segments'][0]['assessment_id'],))
            elif fault == 'cached_text':
                c.execute("UPDATE agent_step SET result_json=json_set(result_json,'$.response_text','already mastered') WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (turn_id,))
            else:
                c.execute("UPDATE agent_step SET result_json=json_remove(result_json,'$.result.spoken_conversation') WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (turn_id,))
            c.commit()
            from home_study_agent.document_check_followup import sources
            from home_study_agent.document_revisit import retained
            assert first in sources(c, retained(c, j['cid'], parent['child_id']))
        calls = len(j['calls'])
        result = j['client'].post('/v1/agent-turns/'+turn_id+'/execute')
        assert result.status_code in {409,422}, result.text
        assert first not in result.text and 'already mastered' not in result.text
        assert len(j['calls']) == calls
