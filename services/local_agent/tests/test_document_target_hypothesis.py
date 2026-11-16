"""Unmatched original -> local target proposal -> spoken choice -> actual probe."""
from contextlib import contextmanager
import json

from fastapi.testclient import TestClient
import pytest

from home_study_agent.app import create_app
from test_diagnostic_prompt_http import environment,CHILD
import test_document_goal_http as original
from test_document_revisit import next_day,offer,say
from test_voice_runtime_binding import capture,interpret,submit


@contextmanager
def proposed(environment,monkeypatch):
    monkeypatch.setattr(original,'BODY','计算 (18+7)×4，只回答一个整数。')
    generator=original.journey.__wrapped__(environment,monkeypatch)
    j=next(generator)
    try:
        next_day(j,monkeypatch)
        offer(j)
        say(j,monkeypatch,'先回顾原题','start','hypothesis-original')
        result=say(j,monkeypatch,'换一道同类新题试试','independent_check','hypothesis-proposal')
        assert result['execution']['result']['status']=='target_choice'
        assert not result['execution']['result'].get('diagnostic_prompt')
        j['cid']=result['execution']['result']['commitment_id']
        yield j
    finally:
        generator.close()


def test_spoken_target_choice_is_not_original_alignment_or_mastery(environment,monkeypatch):
    with proposed(environment,monkeypatch) as j:
        with j['db'].connect() as c:
            payload=json.loads(c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',(j['cid'],)).fetchone()[0])
            hypothesis=payload['independent_target_hypothesis']
            assert hypothesis['alignment_verified'] is False
            assert hypothesis['mastery_update_allowed'] is False
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0]==0
        selected=say(j,monkeypatch,'选第一项','select_1','hypothesis-choice')
        check=selected['execution']['result']['diagnostic_prompt']['prompt']
        assert check['item']['knowledge_node_id']==hypothesis['options'][0]['node_id']
        assert '原题目标尚未最终确认' in selected['execution']['response_text']
        with j['db'].connect() as c:
            source=c.execute('SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?',(check['diagnostic_item_id'],)).fetchone()
            answer=json.loads(source[0])['exact_answers'][0]
        text='我的答案是'+answer
        voice=capture(j['client'],j['settings'],monkeypatch,j['sid'],text,check,'hypothesis-answer-voice')
        turn=interpret(j['client'],j['sid'],text,'hypothesis-answer',voice)
        response=submit(j['client'],j['sid'],check,turn,answer)
        assert response.status_code==200,response.text
        assert response.json()['attempt']['score_status']=='verified_local'
        assert response.json()['attempt']['correctness']==1
        status=say(j,monkeypatch,'刚才新题怎么样','status','hypothesis-result')
        assert '这道新题回答正确' in status['execution']['response_text']
        with j['db'].connect() as c:
            payload=json.loads(c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',(j['cid'],)).fetchone()[0])
            assert payload['independent_target_hypothesis']['alignment_verified'] is False
            assert not payload.get('independent_mapping')
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==1
            assert c.execute('SELECT count(*) FROM mistake_case').fetchone()[0]==0
            c.execute("UPDATE agent_commitment SET payload_json=json_set(payload_json,'$.independent_target_hypothesis.selected_index',99) WHERE commitment_id=?",(j['cid'],))
            c.commit()
        changed=say(j,monkeypatch,'刚才的检查依据还可靠吗','status','hypothesis-invalid-proof')
        assert '暂不据此判断对错' in changed['execution']['response_text']


def test_decline_and_unclear_choice_do_not_create_a_probe(environment,monkeypatch):
    with proposed(environment,monkeypatch) as j:
        unclear=say(j,monkeypatch,'我还没想好','unclear','hypothesis-unsure')
        assert unclear['execution']['result']['status']=='target_choice'
        invalid=say(j,monkeypatch,'选第三项','select_3','hypothesis-missing-option')
        assert invalid['execution']['result']['status']=='target_choice'
        declined=say(j,monkeypatch,'都不对先别选','resume_decline','hypothesis-decline')
        assert declined['execution']['result']['status']=='waiting_answer'
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0
            row=c.execute('SELECT * FROM agent_commitment WHERE commitment_id=?',(j['cid'],)).fetchone()
            assert row['status']=='pending'
            assert json.loads(row['payload_json'])['independent_target_hypothesis']['status']=='declined'


def test_restart_choice_and_replay_preserve_one_probe(environment,monkeypatch):
    with proposed(environment,monkeypatch) as j:
        old=j['client']
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as client:
            j['client']=client
            selected=say(j,monkeypatch,'第一项吧','select_1','hypothesis-restart-choice')
            replay=client.post('/v1/agent-turns/'+selected['turn']['agent_turn_id']+'/execute')
            assert replay.status_code==200,replay.text
            assert not replay.json()['execution']['result'].get('diagnostic_prompt')
        j['client']=old
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0]==1
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0


def test_withdrawn_candidate_can_be_declined_without_sticking_the_voice_lane(environment,monkeypatch):
    with proposed(environment,monkeypatch) as j:
        with j['db'].connect() as c:
            value=json.loads(c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',(j['cid'],)).fetchone()[0])
            option=value['independent_target_hypothesis']['options'][0]
            c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?",(option['item_id'],))
            c.commit()
        selected=say(j,monkeypatch,'选第一项','select_1','hypothesis-stale-choice')
        assert selected['execution']['result']['status']=='waiting_answer'
        assert '来源发生了变化' in selected['execution']['response_text']
        assert not selected['execution']['result'].get('diagnostic_prompt')
        assert say(j,monkeypatch,'原题还在吗','status','hypothesis-after-stale')['execution']['result']['status']=='waiting_answer'


def test_changed_choice_voice_invalidates_the_probe_before_scoring(environment,monkeypatch):
    with proposed(environment,monkeypatch) as j:
        selected=say(j,monkeypatch,'第一项','select_1','hypothesis-tamper-choice')
        prompt=selected['execution']['result']['diagnostic_prompt']['prompt']
        with j['db'].connect() as c:
            c.execute("UPDATE voice_input SET transcript='不是第一项' WHERE voice_input_id=?",(selected['turn']['action']['voice_input_id'],))
            c.commit()
        result=j['client'].post(f'/v1/sessions/{j["sid"]}/diagnostic-prompt/answer',json={
            'prompt_id':prompt['prompt_id'],'item_version':prompt['item_version'],'answer':'42','client_action_id':'hypothesis-invalid-answer'})
        assert result.status_code==200,result.text
        assert result.json()['status']=='invalidated'
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0


def test_selection_and_prompt_rollback_together_then_retry_once(environment,monkeypatch):
    from home_study_agent import document_independent_check as checks
    with proposed(environment,monkeypatch) as j:
        turn=say(j,monkeypatch,'选第一项','select_1','hypothesis-atomic-choice',execute=False)
        begin=checks.begin
        def fail_after_prompt(*args):
            result=begin(*args)
            assert result[0]
            raise RuntimeError('INJECTED_TARGET_SAVE_FAILURE')
        monkeypatch.setattr(checks,'begin',fail_after_prompt)
        path='/v1/agent-turns/'+turn['agent_turn_id']+'/execute'
        with pytest.raises(RuntimeError,match='INJECTED_TARGET_SAVE_FAILURE'):
            j['client'].post(path)
        with j['db'].connect() as c:
            payload=json.loads(c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',(j['cid'],)).fetchone()[0])
            assert payload['independent_target_hypothesis']['status']=='proposed'
            assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0]==0
        monkeypatch.setattr(checks,'begin',begin)
        retried=j['client'].post(path)
        assert retried.status_code==200,retried.text
        assert retried.json()['execution']['result']['diagnostic_prompt']
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0]==1


def test_deleting_learning_clears_hypothesis_only_for_its_child(environment,monkeypatch):
    with proposed(environment,monkeypatch) as j:
        sentinel=json.dumps({'independent_target_hypothesis':{'options':['other-child-target']}})
        with j['db'].connect() as c:
            c.execute('''INSERT INTO agent_commitment(commitment_id,child_id,commitment_type,due_at,status,payload_json,created_at,client_action_id)
                VALUES ('other-hypothesis','child-junior-3','reminder','2026-10-01','pending',?,'2026-09-13','other-target-action')''',(sentinel,))
            c.commit()
        result=j['client'].app.state.privacy_lifecycle.delete_child_scope(CHILD,'learning','确认删除','erase-hypothesis')
        assert result['status']=='completed'
        with j['db'].connect() as c:
            row=c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',(j['cid'],)).fetchone()
            assert row is None or 'independent_target_hypothesis' not in json.loads(row[0])
            assert c.execute("SELECT payload_json FROM agent_commitment WHERE commitment_id='other-hypothesis'").fetchone()[0]==sentinel


def test_local_candidates_respect_grade_and_parent_review_authority(environment,monkeypatch):
    from home_study_agent.document_revisit import retained
    from home_study_agent.document_target_hypothesis import candidates
    with proposed(environment,monkeypatch) as j:
        with j['db'].connect() as c:
            packet=retained(c,j['cid'],CHILD)
            result=candidates(c,packet)
            assert result and all('四年级' in c.execute('SELECT grade_band FROM knowledge_node WHERE knowledge_node_id=?',
                (o['node_id'],)).fetchone()[0] for o in result['options'])
            assert candidates(c,({**packet[0],'child_id':'child-junior-3'},*packet[1:])) is None
            packet[1]['independent_mapping']={'decision':'revoked'}
            assert candidates(c,packet) is None
        # Local source titles and original content are absent from semantic requests.
        request=json.dumps(j['calls'][-1],ensure_ascii=False)
        assert original.BODY not in request
        assert all(o['title'] not in request and o['node_id'] not in request for o in result['options'])


@pytest.mark.parametrize('body,expected',[
    ('小明出生在江苏南京，只回答一个整数。',False),
    ('计算 (18+7)×4。',True),
])
def test_answer_format_is_not_the_evidence_for_a_target(environment,monkeypatch,body,expected):
    from home_study_agent.document_revisit import retained
    from home_study_agent.document_target_hypothesis import candidates
    with proposed(environment,monkeypatch) as j:
        with j['db'].connect() as c:
            packet=retained(c,j['cid'],CHILD)
            changed=(*packet[:4],{**packet[4],'confirmed_text':body},packet[5])
            assert bool(candidates(c,changed)) is expected
