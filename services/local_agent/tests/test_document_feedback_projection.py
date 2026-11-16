"""Quoted feedback may request explanation but cannot acquire control authority."""
import json

import pytest

from home_study_agent.document_feedback_projection import parse, project, KEY
from home_study_agent.document_revisit import focus
from test_document_open_check import opened
from test_document_open_followup import evaluated
from test_document_revisit import say
from test_voice_runtime_binding import capture, interpret

PRIVATE = 'Synthetic reference anchored to the saved response.'
PROJECTED = '你说[本地引用]，为什么？'


def query(j, monkeypatch, key, *, role='question'):
    text = '你说'+PRIVATE+'，为什么？'
    voice = capture(j['client'],j['settings'],monkeypatch,j['sid'],text,key=key+'-voice')
    j['responses'].append(json.dumps(dict(role=role,control=None,quote=PROJECTED,confidence=.99)))
    turn = interpret(j['client'],j['sid'],text,key,voice)
    assert turn['runtime_capability']=='learning.review_revisit', turn
    assert json.loads(j['calls'][-1]['messages'][1]['content'])=={'transcript':PROJECTED}
    return turn


@pytest.mark.parametrize('fault', ['source', 'answer', 'voice', 'criteria', 'forged_cancel'])
def test_change_after_classification_cannot_play_or_execute(tmp_path, monkeypatch, fault):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        prompt, saved, follow = evaluated(j,monkeypatch,'negative-first')
        turn = query(j,monkeypatch,'negative-query')
        with j['db'].connect() as c:
            if fault=='source':
                c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (prompt['diagnostic_item_id'],))
            elif fault=='answer':
                c.execute("UPDATE diagnostic_attempt SET submitted_answer='changed' WHERE attempt_id=?", (saved['attempt']['attempt_id'],))
            elif fault=='voice':
                c.execute("UPDATE voice_input SET transcript='changed' WHERE voice_input_id=?", (turn['action']['voice_input_id'],))
            elif fault=='criteria':
                c.execute("UPDATE agent_objective SET success_criteria_json='[\"changed\"]' WHERE objective_id=(SELECT objective_id FROM agent_plan WHERE agent_turn_id=?)",
                    (follow['feedback_turn_id'],))
            else:
                c.execute("""UPDATE voice_control_intent_request SET decision_json=json_set(decision_json,'$.meaning','cancel')
                    WHERE request_key=?""", (turn['action']['document_revisit_request'],))
            c.commit()
        count = len(j['calls'])
        result = j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert result.status_code in {409,422}, result.text
        assert len(j['calls'])==count
        with j['db'].connect() as c:
            assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?', (j['cid'],)).fetchone()[0] in {'pending','due'}
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==1


def test_source_withdrawal_blocks_saved_explanation_replay(tmp_path,monkeypatch):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        prompt, _, _ = evaluated(j,monkeypatch,'replay-first')
        turn = query(j,monkeypatch,'replay-query')
        first = j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert first.status_code==200, first.text
        with j['db'].connect() as c:
            c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (prompt['diagnostic_item_id'],))
            c.commit()
        count=len(j['calls'])
        replay = j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert replay.status_code in {409,422}, replay.text
        assert len(j['calls'])==count


@pytest.mark.parametrize('role', ['control','mixed','answer','unknown'])
def test_projected_nonquestion_never_changes_the_practice(tmp_path,monkeypatch,role):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        evaluated(j,monkeypatch,'role-first')
        turn=query(j,monkeypatch,'role-query',role=role)
        response=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert response.status_code==200,response.text
        assert '没有改动复习安排' in response.json()['execution']['response_text']
        assert response.json()['execution']['result']['status']=='check_feedback'


def test_projection_preserves_outside_text_and_requires_current_owner(tmp_path,monkeypatch):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        evaluated(j,monkeypatch,'scope-first')
        with j['db'].connect() as c:
            c.execute('BEGIN')
            child=c.execute('SELECT child_id FROM daily_session WHERE session_id=?',(j['sid'],)).fetchone()[0]
            ref=focus(c,j['sid'],child,'scope')
            binding={'document_revisit':ref}
            text='不要取消学习。你说“'+PRIVATE+'”，又说'+PRIVATE+'，这是什么意思？'
            value=project(c,child,j['sid'],text,binding)
            assert value and value[0]=='不要取消学习。你说[本地引用]，又说[本地引用]，这是什么意思？'
            assert len(value[1]['spans'])==2 and value[1]['cloud_transmission_allowed'] is False
            assert project(c,'other-child',j['sid'],text,binding) is None
            assert project(c,child,'other-session',text,binding) is None
            assert project(c,child,j['sid'],'普通新问题，没有引用',binding) is None
            assert project(c,child,j['sid'],'“未知私有内容”是什么意思',binding) is None
            partial=project(c,child,j['sid'],'你说anchored to the saved response，这是什么意思？',binding)
            assert partial and partial[0]=='你说[本地引用]，这是什么意思？'


@pytest.mark.parametrize('stage', ['before_dispatch','after_response'])
def test_source_change_around_physical_dispatch_invalidates_projection(tmp_path,monkeypatch,stage):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        prompt, _, _ = evaluated(j,monkeypatch,'race-first')
        complete=j['gateway'].complete
        def withdraw():
            with j['db'].connect() as c:
                c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?",(prompt['diagnostic_item_id'],))
                c.commit()
        async def racing(request):
            if stage=='before_dispatch':
                withdraw()
            result=await complete(request)
            if stage=='after_response':
                withdraw()
            return result
        monkeypatch.setattr(j['gateway'],'complete',racing)
        text='你说'+PRIVATE+'，为什么？'
        voice=capture(j['client'],j['settings'],monkeypatch,j['sid'],text,key='race-voice')
        before=len(j['calls'])
        j['responses'].append(json.dumps(dict(role='question',control=None,quote=PROJECTED,confidence=.99)))
        turn=interpret(j['client'],j['sid'],text,'race-query',voice)
        assert len(j['calls'])==before+(stage=='after_response')
        with j['db'].connect() as c:
            journal=c.execute('SELECT decision_json FROM voice_control_intent_request WHERE request_key=?',
                (turn['action']['document_revisit_request'],)).fetchone()
            assert json.loads(journal[0])['status']=='unavailable'
        response=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert response.status_code in {409,422},response.text
