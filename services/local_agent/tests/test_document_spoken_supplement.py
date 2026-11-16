"""A new answer spoken after feedback is not a request to repeat old advice."""
import json
from pathlib import Path
import pytest

from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from test_document_open_check import opened, evaluation, snapshot
from test_document_open_followup import evaluated
from test_document_revisit import say
from test_voice_runtime_binding import capture, interpret


def remove_generated_recordings(j, tmp_path):
    # Simulate raw-file expiry only inside this test's disposable media store.
    with j['db'].connect() as c:
        paths = c.execute('SELECT local_path FROM voice_input WHERE local_path IS NOT NULL').fetchall()
        assert paths
        for row in paths:
            path = Path(row[0])
            assert path.resolve().is_relative_to(tmp_path.resolve()) and path.is_file()
            path.unlink()
        c.execute('UPDATE voice_input SET local_path=NULL')
        c.commit()


@pytest.mark.parametrize('raw_voice', [True, False, 'removed'])
def test_direct_supplement_saved_evaluated_without_repeating_or_clicking(tmp_path, monkeypatch, raw_voice):
    with opened(tmp_path, monkeypatch, 'spoken_explanation', raw_voice=raw_voice is not False) as j:
        parent, first, _ = evaluated(j, monkeypatch, 'supplement-parent')
        old_answer = j['answer']
        j['answer'] = '我补充一下，把平均每人的数量乘回人数，应该等于原来的总数，这样就检查了条件。'
        # Classify the actual new utterance, then supply the controlled grader.
        j['responses'].append(json.dumps(dict(meaning='answer',quote=j['answer'],time=None,confidence=.99)))
        voice=capture(j['client'],j['settings'],monkeypatch,j['sid'],j['answer'],parent,'direct-supplement-voice')
        turn=interpret(j['client'],j['sid'],j['answer'],'direct-supplement',voice)
        j['responses'].append(evaluation(j, parent))
        result = j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert result.status_code == 200, result.text
        payload = result.json()['execution']['result']
        assert payload.get('practice_kind') == 'spoken_supplement', payload
        assert payload['diagnostic_prompt']['status'] == 'answered'
        new = payload['diagnostic_prompt']['prompt']
        assert new['prompt_id'] != parent['prompt_id']
        assert new['diagnostic_item_id'] == parent['diagnostic_item_id']
        assert payload['supplement_evaluation']['formative_only'] is True
        outbound=json.loads(j['calls'][-1]['messages'][1]['content'])
        assert set(outbound)=={'question','response','rubric'}
        assert outbound['response']==j['answer']
        assert parent['prompt_id'] not in json.dumps(j['calls'][-1])
        assert first['attempt']['attempt_id'] not in json.dumps(j['calls'][-1])
        with j['db'].connect() as c:
            rows = c.execute('''SELECT a.*,e.hint_level FROM diagnostic_attempt a
                JOIN learning_evidence e USING(evidence_id) ORDER BY a.rowid''').fetchall()
            assert len(rows) == 2
            assert rows[0]['submitted_answer'] == old_answer
            assert rows[1]['submitted_answer'] == j['answer'] and rows[1]['hint_level'] >= 1
            assert rows[1]['correctness'] is None
            assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0] == 2
            captured_at = c.execute('SELECT created_at FROM voice_capture_binding WHERE voice_input_id=?',
                (turn['action']['voice_input_id'],)).fetchone()[0]
            created = c.execute('SELECT created_at FROM diagnostic_prompt WHERE prompt_id=?',(new['prompt_id'],)).fetchone()[0]
            assert captured_at <= created  # Real capture precedes its derivative prompt.
            if not raw_voice:
                assert not c.execute('SELECT 1 FROM voice_input WHERE local_path IS NOT NULL AND voice_input_id=?',
                    (turn['action']['voice_input_id'],)).fetchone()
                from home_study_agent.pet_speaking_origin import original_answer_voice, original_answer_transcript
                row = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?', (new['prompt_id'],)).fetchone()
                capture_hash = c.execute('SELECT wav_sha256 FROM voice_capture_binding WHERE voice_input_id=?',
                    (turn['action']['voice_input_id'],)).fetchone()[0]
                media = dict(child_id=row['child_id'], session_id=row['origin_session_id'], prompt_id=row['prompt_id'],
                    item_version=row['item_version'], voice_input_id=turn['action']['voice_input_id'])
                assert original_answer_transcript(c, row, rows[1], media, capture_hash)
                with pytest.raises(ValueError):
                    original_answer_voice(c, row, rows[1], media, capture_hash)
        assert snapshot(j)['practice']['independent_results'][-1]['prompt_id'] == new['prompt_id']
        if raw_voice == 'removed':
            remove_generated_recordings(j, tmp_path)
        count = len(j['calls'])
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
            replay = restarted.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
            assert replay.status_code == 200, replay.text
            assert replay.json()['execution']['replay_audio_allowed'] is False
            assert replay.json()['execution']['result']['supplement_evaluation_status'] == 'completed', replay.text
            assert replay.json()['execution']['result']['supplement_evaluation']['formative_only'] is True
            assert len(j['calls']) == count


@pytest.mark.parametrize('raw_voice', [True, False])
@pytest.mark.parametrize('fault', ['source','voice','meaning','capture_cancelled','binding_removed'])
def test_direct_supplement_rechecks_before_writing(tmp_path, monkeypatch, fault, raw_voice):
    with opened(tmp_path, monkeypatch, 'spoken_explanation', raw_voice=raw_voice) as j:
        parent, _, _ = evaluated(j, monkeypatch, 'invalid-supplement-parent')
        text='我补充一下，要把得到的数量乘回人数检查总数。'
        turn=say(j,monkeypatch,text,'answer','invalid-supplement',execute=False)
        with j['db'].connect() as c:
            if fault=='source':
                c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?",(parent['diagnostic_item_id'],))
            elif fault=='voice':
                c.execute("UPDATE voice_input SET transcript='changed speech' WHERE voice_input_id=?",(turn['action']['voice_input_id'],))
            elif fault == 'capture_cancelled':
                c.execute("UPDATE voice_capture_binding SET status='cancelled' WHERE voice_input_id=?",(turn['action']['voice_input_id'],))
            elif fault == 'binding_removed':
                c.execute('DELETE FROM voice_capture_binding WHERE voice_input_id=?',(turn['action']['voice_input_id'],))
            else:
                c.execute("UPDATE voice_control_intent_request SET decision_json=json_set(decision_json,'$.meaning','retry_answer') WHERE request_key=?",
                    (turn['action']['document_revisit_request'],))
            c.commit()
        calls=len(j['calls'])
        result=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert result.status_code in {409,422},result.text
        assert len(j['calls'])==calls
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==1
            assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0]==1


@pytest.mark.parametrize('raw_voice', [True, False, 'removed'])
def test_saved_supplement_recovers_evaluation_without_new_speech(tmp_path, monkeypatch, raw_voice):
    from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator
    with opened(tmp_path, monkeypatch, 'spoken_explanation', raw_voice=raw_voice is not False) as j:
        parent, _, _ = evaluated(j, monkeypatch, 'recovery-supplement-parent')
        j['answer']='我补充一下，用总数和人数反过来检验计算过程是否符合题意。'
        turn=say(j,monkeypatch,j['answer'],'answer','recovery-supplement',execute=False)
        async def unavailable(*args):
            raise ConnectionError('synthetic unavailable before evaluation claim')
        with monkeypatch.context() as fault:
            fault.setattr(LearningOutcomeEvaluator,'evaluate',unavailable)
            saved=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
            assert saved.status_code==200,saved.text
            assert saved.json()['execution']['result']['supplement_evaluation_status']=='pending'
        if raw_voice == 'removed':
            remove_generated_recordings(j, tmp_path)
        j['responses'].append(evaluation(j,parent))
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
            result=restarted.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
            assert result.status_code==200,result.text
            assert result.json()['execution']['result']['supplement_evaluation_status']=='completed',result.text
            assert result.json()['execution']['replay_audio_allowed'] is False
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==2
            assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0]==2


def test_replayed_supplement_cannot_return_withdrawn_feedback(tmp_path, monkeypatch):
    with opened(tmp_path, monkeypatch, 'spoken_explanation') as j:
        parent, _, _=evaluated(j,monkeypatch,'replay-withdraw-parent')
        j['answer']='我还要反过来乘一次，检验所得结果是否满足题目条件。'
        turn=say(j,monkeypatch,j['answer'],'answer','replay-withdraw-answer',execute=False)
        j['responses'].append(evaluation(j,parent))
        initial=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert initial.status_code==200 and initial.json()['execution']['result']['supplement_evaluation_status']=='completed',initial.text
        with j['db'].connect() as c:
            c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?",(parent['diagnostic_item_id'],))
            c.commit()
        calls=len(j['calls'])
        replay=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert replay.status_code==200,replay.text
        result=replay.json()['execution']
        assert not result['result'].get('supplement_evaluation')
        assert result['result']['supplement_evaluation_status']=='pending'
        assert result['replay_audio_allowed'] is False and len(j['calls'])==calls
