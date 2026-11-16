"""Clarification authority is revoked, rolled back and deleted with its evidence."""
import json
import pytest

from home_study_agent import document_target_hypothesis as targets
from test_diagnostic_prompt_http import environment,CHILD
from test_document_revisit import say
from test_document_target_clarification import searching,describe,saved


def test_clarification_and_search_roll_back_together_and_replay_once(environment,monkeypatch):
    with searching(environment,monkeypatch) as j:
        before=saved(j)
        turn=describe(j,monkeypatch,'它先合并数量再做乘法，想知道运算次序','整数混合运算','clarify-rollback',execute=False)
        original=targets.offer
        def fail_after_search(*args,**kwargs):
            result=original(*args,**kwargs)
            assert '学习目标' in result
            raise RuntimeError('INJECTED_CLARIFICATION_FAILURE')
        monkeypatch.setattr(targets,'offer',fail_after_search)
        path='/v1/agent-turns/'+turn['agent_turn_id']+'/execute'
        with pytest.raises(RuntimeError,match='INJECTED_CLARIFICATION_FAILURE'):
            j['client'].post(path)
        assert saved(j)==before
        with j['db'].connect() as c:
            action=json.loads(c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?',(turn['agent_turn_id'],)).fetchone()[0])
            assert 'document_target_clarification' not in action
        monkeypatch.setattr(targets,'offer',original)
        result=j['client'].post(path)
        assert result.status_code==200,result.text
        assert result.json()['execution']['result']['status']=='target_choice'
        assert len(saved(j)[targets.KEY]['clarifications'])==1
        assert j['client'].post(path).status_code==200
        assert len(saved(j)[targets.KEY]['clarifications'])==1


def test_revoked_description_after_selection_cannot_support_scoring(environment,monkeypatch):
    with searching(environment,monkeypatch) as j:
        description=describe(j,monkeypatch,'它要检查四则运算次序','整数混合运算','clarify-score-proof')
        selected=say(j,monkeypatch,'第一个','select_1','clarify-score-selection')
        prompt=selected['execution']['result']['diagnostic_prompt']['prompt']
        with j['db'].connect() as c:
            c.execute("UPDATE voice_input SET transcript='原描述撤回' WHERE voice_input_id=?",
                (description['turn']['action']['voice_input_id'],))
            c.commit()
        response=j['client'].post(f'/v1/sessions/{j["sid"]}/diagnostic-prompt/answer',json={
            'prompt_id':prompt['prompt_id'],'item_version':prompt['item_version'],
            'answer':'42','client_action_id':'clarify-score-after-revoke'})
        assert response.status_code==200,response.text
        assert response.json()['status']=='invalidated'
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0


@pytest.mark.parametrize('scope',['learning','media','tutor','knowledge'])
def test_clarification_history_cleanup_does_not_delete_other_child(environment,monkeypatch,scope):
    with searching(environment,monkeypatch) as j:
        describe(j,monkeypatch,'补充一下是运算的先后次序','整数混合运算','clarify-delete-source')
        sentinel=json.dumps({targets.KEY:{'clarifications':[{'query':'another child query'}],
            'superseded_clarifications':[[{'query':'another child history'}]]}})
        with j['db'].connect() as c:
            c.execute('''INSERT INTO agent_commitment(commitment_id,child_id,commitment_type,due_at,status,payload_json,created_at,client_action_id)
                VALUES ('other-clarification','child-junior-3','reminder','2026-10-01','pending',?,'2026-09-13','other-clarification-action')''',(sentinel,))
            c.commit()
        result=j['client'].app.state.privacy_lifecycle.delete_child_scope(CHILD,scope,'确认删除','clear-clarification-'+scope)
        assert result['status']=='completed'
        with j['db'].connect() as c:
            row=c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',(j['cid'],)).fetchone()
            assert not row or targets.KEY not in json.loads(row[0])
            assert c.execute("SELECT payload_json FROM agent_commitment WHERE commitment_id='other-clarification'").fetchone()[0]==sentinel
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
