"""A real scored independent check must return to the original voice journey."""
import json

import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app

from test_document_independent_check import environment, mapped, say, CHILD
from test_voice_runtime_binding import capture, interpret, submit


def answer_check(j,monkeypatch,answer='42'):
    issued=say(j,monkeypatch,'换道新题考我','independent_check','first-followup-check')
    prompt=issued['execution']['result']['diagnostic_prompt']['prompt']
    text='我的答案是'+answer
    voice=capture(j['client'],j['settings'],monkeypatch,j['sid'],text,prompt,'followup-answer-voice')
    turn=interpret(j['client'],j['sid'],text,'followup-answer',voice)
    result=submit(j['client'],j['sid'],prompt,turn,answer)
    assert result.status_code==200,result.text
    assert result.json()['status']=='answered',result.text
    return issued,prompt,turn,result.json()


@pytest.mark.parametrize('answer',['42','43'])
def test_answered_check_can_continue_by_voice_without_returning_to_original(environment,monkeypatch,answer):
    with mapped(environment,monkeypatch) as j:
        issued,prompt,turn,answered=answer_check(j,monkeypatch,answer)
        cid=issued['execution']['result']['commitment_id']
        with j['db'].connect() as c:
            work=json.loads(c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',(cid,)).fetchone()[0])['practice']
            assert work['phase']=='check_feedback'
            assert work['last_turn_id']==turn['agent_turn_id']
            assert work['independent_results'][-1]['attempt_id']==answered['attempt']['attempt_id']
        status=say(j,monkeypatch,'刚才那题怎么样','status','check-followup-status')
        message=status['execution']['response_text']
        assert ('这道新题回答正确' if answer=='42' else '这道新题还没有完全答对') in message
        assert '已经掌握' not in message
        helped=say(j,monkeypatch,'请讲讲刚才那道新题','help','check-followup-help')
        assert prompt['item']['prompt'] in helped['execution']['response_text']
        assert not helped['execution']['result'].get('feedback_response_turn_id')
        another=say(j,monkeypatch,'再给我一道新题','independent_check','second-followup-check')
        new=another['execution']['result']['diagnostic_prompt']['prompt']
        assert new['prompt_id']!=prompt['prompt_id']
        assert new['item']['prompt']!=prompt['item']['prompt']
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==1
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==1
            assert c.execute('SELECT count(*) FROM mistake_case').fetchone()[0]==(answer=='43')


def test_restart_then_spoken_finish_preserves_future_reviews(environment,monkeypatch):
    with mapped(environment,monkeypatch) as j:
        issued,prompt,turn,answered=answer_check(j,monkeypatch)
        with j['db'].connect() as c:
            reviews=[tuple(r) for r in c.execute('SELECT * FROM review_task ORDER BY review_task_id')]
        client=j['client']
        j['finish_client']()  # Preserve a genuine sequential restart, not two live owners.
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
            j['client']=restarted
            done=say(j,monkeypatch,'这道题先回顾到这里','finish_check','finish-original-check')
            assert done['execution']['result']['status']=='check_closed'
            replay=restarted.post('/v1/agent-turns/'+issued['turn']['agent_turn_id']+'/execute')
            assert replay.status_code==200,replay.text
            resumed=restarted.post(f'/v1/sessions/{j["sid"]}/diagnostic-prompt/resume',json={k:prompt[k] for k in ('prompt_id','item_version')})
            assert resumed.status_code==200,resumed.text
            assert resumed.json()['status']=='answered'
        j['client']=client
        with j['db'].connect() as c:
            row=c.execute('SELECT * FROM agent_commitment WHERE commitment_id=?',(issued['execution']['result']['commitment_id'],)).fetchone()
            assert row['status']=='completed'
            assert json.loads(row['payload_json'])['practice']['phase']=='check_closed'
            assert [tuple(r) for r in c.execute('SELECT * FROM review_task ORDER BY review_task_id')]==reviews
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==1


def test_old_answer_replay_cannot_replace_the_next_pending_check(environment,monkeypatch):
    with mapped(environment,monkeypatch) as j:
        issued,prompt,turn,answered=answer_check(j,monkeypatch)
        another=say(j,monkeypatch,'再考一道','independent_check','repeat-next-check')
        new=another['execution']['result']['diagnostic_prompt']['prompt']
        resumed=j['client'].post(f'/v1/sessions/{j["sid"]}/diagnostic-prompt/resume',json={k:prompt[k] for k in ('prompt_id','item_version')})
        assert resumed.status_code==200,resumed.text
        with j['db'].connect() as c:
            work=json.loads(c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',
                (issued['execution']['result']['commitment_id'],)).fetchone()[0])['practice']
            assert work['phase']=='checking_new_question'
            assert work['independent_prompt_id']==new['prompt_id']
            assert len(work['independent_results'])==1


@pytest.mark.parametrize('change',['source','receipt','mapping'])
def test_withdrawn_result_cannot_be_explained_as_current_authority(environment,monkeypatch,change):
    from test_diagnostic_prompt_http import ITEM
    with mapped(environment,monkeypatch) as j:
        issued,prompt,turn,answered=answer_check(j,monkeypatch)
        with j['db'].connect() as c:
            if change=='receipt':
                c.execute("UPDATE diagnostic_prompt SET attempt_id=NULL WHERE prompt_id=?",(prompt['prompt_id'],))
            else:
                c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?",(ITEM if change=='mapping' else prompt['diagnostic_item_id'],))
            c.commit()
        result=say(j,monkeypatch,'解释刚才那道新题','help','withdrawn-followup')
        assert '暂不据此判断对错' in result['execution']['response_text']
        assert prompt['item']['prompt'] not in result['execution']['response_text']
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==1


def test_followup_failure_rolls_back_score_and_resumes_saved_answer_once(environment,monkeypatch):
    from home_study_agent import document_check_followup as followup
    with mapped(environment,monkeypatch) as j:
        record=followup.record
        def fail_after_record(*args):
            record(*args)
            raise RuntimeError('INJECTED_FOLLOWUP_STORAGE_FAILURE')
        monkeypatch.setattr(followup,'record',fail_after_record)
        with pytest.raises(RuntimeError,match='INJECTED_FOLLOWUP_STORAGE_FAILURE'):
            answer_check(j,monkeypatch)
        with j['db'].connect() as c:
            prompt=c.execute('SELECT * FROM diagnostic_prompt').fetchone()
            assert prompt['status']=='answer_saved'
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0
        monkeypatch.setattr(followup,'record',record)
        resumed=j['client'].post(f'/v1/sessions/{j["sid"]}/diagnostic-prompt/resume',json={k:prompt[k] for k in ('prompt_id','item_version')})
        assert resumed.status_code==200,resumed.text
        assert resumed.json()['status']=='answered'
        assert say(j,monkeypatch,'刚才那题怎么样','status','resumed-check-feedback')['execution']['result']['status']=='check_feedback'
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==1


def test_acknowledgement_and_exhausted_supply_keep_the_last_actual_result(environment,monkeypatch):
    from home_study_agent import document_check_supply as supply
    from home_study_agent.local_supply import LocalSupplyError
    with mapped(environment,monkeypatch) as j:
        issued,prompt,turn,answered=answer_check(j,monkeypatch)
        acknowledged=say(j,monkeypatch,'谢谢我听懂了','unclear','check-thanks')
        assert acknowledged['execution']['result']['status']=='check_feedback'
        def exhausted(*args):
            raise LocalSupplyError('LOCAL_TEMPLATE_EXHAUSTED')
        monkeypatch.setattr(supply,'build_candidate',exhausted)
        missing=say(j,monkeypatch,'再考我一道','independent_check','check-no-material')
        assert not missing['execution']['result'].get('diagnostic_prompt')
        status=say(j,monkeypatch,'上一题的结果还在吗','status','previous-check-status')
        assert '这道新题回答正确' in status['execution']['response_text']
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==1
            assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0]==1


def test_learning_erasure_removes_result_links_without_touching_other_child(environment,monkeypatch):
    with mapped(environment,monkeypatch) as j:
        issued,prompt,turn,answered=answer_check(j,monkeypatch)
        sentinel=json.dumps({'practice':{'independent_results':[{'attempt_id':'other-child-result'}]}})
        with j['db'].connect() as c:
            c.execute('''INSERT INTO agent_commitment(commitment_id,child_id,commitment_type,due_at,status,payload_json,created_at,client_action_id)
                VALUES ('other-result','child-junior-3','reminder','2026-10-01','pending',?,'2026-09-13','other-result-action')''',(sentinel,))
            c.commit()
        result=j['client'].app.state.privacy_lifecycle.delete_child_scope(CHILD,'learning','确认删除','erase-independent-result')
        assert result['status']=='completed'
        with j['db'].connect() as c:
            assert c.execute("SELECT payload_json FROM agent_commitment WHERE commitment_id='other-result'").fetchone()[0]==sentinel
            rows=c.execute('SELECT payload_json FROM agent_commitment WHERE child_id=?',(CHILD,)).fetchall()
            assert all(not json.loads(r[0]).get('practice',{}).get('independent_results') for r in rows)
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
