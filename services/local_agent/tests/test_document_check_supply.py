"""Use verified local supply in the original-question voice journey."""
import json

import pytest

from test_document_independent_check import environment, mapped, say, SECOND, CHILD
from test_voice_runtime_binding import capture, interpret, submit
from test_diagnostic_prompt_http import ITEM
from fastapi.testclient import TestClient
from home_study_agent.app import create_app


def exhaust(j):
    with j['db'].connect() as c:
        c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?",(SECOND,))
        c.commit()


def counts(j):
    with j['db'].connect() as c:
        return tuple(c.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in
            ('specialized_supply_job','specialized_supply_candidate','diagnostic_prompt','learning_evidence','mistake_case'))


@pytest.mark.parametrize('exhaustion',['inactive','shown'])
def test_exhausted_catalog_supplies_a_new_question_without_inventing_a_mistake(environment,monkeypatch,exhaustion):
    with mapped(environment,monkeypatch) as j:
        if exhaustion=='inactive':
            with j['db'].connect() as c:
                c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?",(SECOND,))
                c.commit()
        else:
            from test_diagnostic_prompt_http import prepare,binding
            shown=prepare(j['client'],j['sid'],preferred_item_id=SECOND)['prompt']
            assert j['client'].post(f'/v1/sessions/{j["sid"]}/diagnostic-prompt/cancel',json=binding(shown)).status_code==200
        result=say(j,monkeypatch,'换一道没做过的新题考我','independent_check','supply-new-check')
        prompt=result['execution']['result']['diagnostic_prompt']['prompt']
        assert prompt['diagnostic_item_id'].startswith('local-specialized-')
        assert prompt['item']['knowledge_node_id']=='node-p4-math-arithmetic'
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_case').fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0
            source=c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?',(prompt['diagnostic_item_id'],)).fetchone()
            answer=json.loads(source['expected_answer_json'])['exact_answers'][0]
            candidate=c.execute('SELECT validation_json FROM specialized_supply_candidate WHERE diagnostic_item_id=?',(prompt['diagnostic_item_id'],)).fetchone()
            validation=json.loads(candidate[0])
            assert validation['parent_approved'] is False
            assert validation['qualification']=='deterministic_answer_verified_v1'
        text='我的答案是'+answer
        voice=capture(j['client'],j['settings'],monkeypatch,j['sid'],text,prompt,'supplied-answer-voice')
        heard=interpret(j['client'],j['sid'],text,'supplied-answer',voice)
        response=submit(j['client'],j['sid'],prompt,heard,answer)
        assert response.status_code==200,response.text
        assert response.json()['status']=='answered',response.text
        assert response.json()['attempt']['score_status']=='verified_local'
        assert response.json()['attempt']['correctness']==1


def test_supply_prompt_and_journal_are_atomic_and_restart_does_not_supply_twice(environment,monkeypatch):
    from home_study_agent import document_independent_check as check
    from home_study_agent.document_goal_handoff import DocumentGoalError
    with mapped(environment,monkeypatch) as j:
        exhaust(j)
        turn=say(j,monkeypatch,'换一道新题考我','independent_check','supply-rollback',execute=False)
        begin=check.begin
        def interrupted(*args):
            begin(*args)
            raise DocumentGoalError('INJECTED_AFTER_SUPPLY_AND_PREPARE')
        monkeypatch.setattr(check,'begin',interrupted)
        url='/v1/agent-turns/'+turn['agent_turn_id']+'/execute'
        assert j['client'].post(url).status_code==409
        assert counts(j)==(0,0,0,0,0)
        with j['db'].connect() as c:
            action=json.loads(c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?',(turn['agent_turn_id'],)).fetchone()[0])
            assert action['document_independent_check']['status']=='new_material_required'
            assert c.execute("SELECT count(*) FROM diagnostic_item WHERE diagnostic_item_id LIKE 'local-specialized-%'").fetchone()[0]==0
        monkeypatch.setattr(check,'begin',begin)
        response=j['client'].post(url)
        assert response.status_code==200,response.text
        prompt=response.json()['execution']['result']['diagnostic_prompt']['prompt']
        assert counts(j)==(1,1,1,0,0)
        j['finish_client']()  # Preserve a genuine sequential restart, not two live owners.
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
            replay=restarted.post(url)
            assert replay.status_code==200,replay.text
            assert not replay.json()['execution']['result'].get('diagnostic_prompt')
            current=restarted.get(f'/v1/sessions/{j["sid"]}/diagnostic-prompt').json()
            assert current['prompt']['prompt_id']==prompt['prompt_id']
        assert counts(j)==(1,1,1,0,0)


@pytest.mark.parametrize('change',['seed','published_item','candidate'])
def test_supplied_question_loses_scoring_authority_when_source_is_revoked(environment,monkeypatch,change):
    with mapped(environment,monkeypatch) as j:
        exhaust(j)
        result=say(j,monkeypatch,'换道新题考我','independent_check','supply-revoke')
        prompt=result['execution']['result']['diagnostic_prompt']['prompt']
        with j['db'].connect() as c:
            if change=='candidate':
                c.execute("UPDATE specialized_supply_candidate SET status='rejected' WHERE diagnostic_item_id=?",(prompt['diagnostic_item_id'],))
            else:
                c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?",(ITEM if change=='seed' else prompt['diagnostic_item_id'],))
            c.commit()
        response=j['client'].post(f'/v1/sessions/{j["sid"]}/diagnostic-prompt/answer',json={
            'prompt_id':prompt['prompt_id'],'item_version':prompt['item_version'],'answer':'42','client_action_id':'revoked-supply-answer'})
        assert response.status_code==200,response.text
        assert response.json()['status']=='invalidated',response.text
        assert counts(j)==(1,1,1,0,0)


@pytest.mark.parametrize('scope',['learning','all_history'])
def test_learning_deletion_removes_supplied_sources_and_preserves_other_child(environment,monkeypatch,scope):
    from home_study_agent.document_check_supply import deliver
    from home_study_agent.local_supply import validate_item
    with mapped(environment,monkeypatch) as j:
        exhaust(j)
        result=say(j,monkeypatch,'换道新题考我','independent_check','supply-delete')
        item=result['execution']['result']['diagnostic_prompt']['prompt']['diagnostic_item_id']
        with j['db'].connect() as c:
            other=[tuple(r) for r in c.execute("SELECT * FROM child_profile WHERE child_id='child-junior-3'")]
            c.execute('BEGIN IMMEDIATE')
            other_item=deliver(c,'child-junior-3','diag-j3-discriminant-1','other-child-supply')
            other_source=validate_item(c,'child-junior-3',other_item)
            c.commit()
        deleted=j['client'].app.state.privacy_lifecycle.delete_child_scope(CHILD,scope,'确认删除','delete-supplied-learning')
        assert deleted['status']=='completed'
        assert counts(j)==(1,1,0,0,0)
        with j['db'].connect() as c:
            erased=c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?',(item,)).fetchone()
            assert erased['status']=='retired'
            assert erased['prompt']==erased['explanation']==''
            assert erased['expected_answer_json']=='{}'
            assert erased['source_ref']=='deleted_local_supply'
            assert validate_item(c,'child-junior-3',other_item)==other_source
            assert c.execute('SELECT count(*) FROM specialized_supply_candidate WHERE child_id=?',(CHILD,)).fetchone()[0]==0
            assert [tuple(r) for r in c.execute("SELECT * FROM child_profile WHERE child_id='child-junior-3'")]==other
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
