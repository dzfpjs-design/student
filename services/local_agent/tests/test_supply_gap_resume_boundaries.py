"""COPY admission races and multiple real generated obligations."""
import json
from synthetic_unclean_app_process import unclean_app
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.repository import StudyRepository
from home_study_agent.learning_state import LearningStateService
from home_study_agent.task_start_binding import TaskStartBinding
from test_supply_gap_resume_http import supplied, facts, execute, start, CHILD
from test_voice_runtime_binding import capture, interpret


def test_multiple_original_gaps_require_completed_clarification_not_latest(supplied):
    case = supplied
    gaps = [r for r in facts(case)['plan_constraint_decision'] if r['decision']=='deferred']
    assert any(r['source_ref']=='pet-skill-gap:reading' for r in gaps)
    # Isolated catalog recovery of the byte-identical shipped reading seed.
    # No task, prompt, grant or reliable child result is inserted. Parent uses
    # that seed through the real source-options/review/publication contract.
    with case['db'].connect() as c:
        c.execute("UPDATE diagnostic_item SET status='active' WHERE diagnostic_item_id='diag-pet-reading-1'")
        c.commit()
    LearningStateService(case['db']).submit_diagnostic(case['sid'],'diag-pet-reading-1',
        'I do not know this yet.','isolated-reading-reference')
    result = case['client'].get(case['base']+f"/sources/{case['source']['source_id']}/options")
    assert result.status_code==200, result.text
    options = result.json()
    seed = next(s for s in options['seeds'] if s['seed_item_id']=='diag-pet-reading-1')
    draft = case['post'](case['client'],case['base']+'/generate', {
        'action_id':'reading-published-after-gap','expected_version':0,
        'expected_scope_version':case['selected']['version'], 'source':{
            'seed_item_id':seed['seed_item_id'],'validator':seed['validator'],'stage':'parallel',
            'excerpt_id':options['excerpts'][2]['excerpt_id'],
            'requirement_id':case['standard']['requirement_id'],'standard_page':case['standard']['page_number'],
            'standard_quote':case['standard']['quote'],'difficulty':1,'explanation':'Original exact reference, no skill claim'}})
    for version, operation in enumerate(('validate','review','publish'),1):
        body = dict(expected_version=version,action_id='reading-'+operation)
        if operation=='review':
            body.update(decision='approve',note='Read the source and original reference answer.')
        published = case['post'](case['client'],case['base']+f"/{draft['candidate_id']}/{operation}",body)
    assert published['eligible']
    with case['db'].connect() as c:
        candidates=TaskStartBinding.resume_snapshot(c,case['sid'],CHILD)
        assert candidates['status']=='ambiguous'
        assert {i['source_ref'] for i in candidates['items']}=={
            case['published']['diagnostic_item_id'],published['diagnostic_item_id']}
    before = facts(case)
    clarification = case['say']('which-original','题都补好了，继续刚才的任务吧')
    assert clarification['action']['source']=='task_resume_clarification', clarification
    assert len(clarification['action']['task_resume_choices'])==2
    assert facts(case)==before
    completed = case['post'](case['client'],f"/v1/agent-turns/{clarification['agent_turn_id']}/execute",{})
    case['speech']['selector']['task_kind']='written'
    selected = case['say']('which-writing','先继续刚才那项写作练习')
    result = execute(case,selected)
    assert result.status_code==200, result.text
    after=facts(case)
    assert len(after['plan_item'])==len(before['plan_item'])+1
    assert after['plan_item'][-1]['source_ref']==case['published']['diagnostic_item_id']
    assert not any(p['diagnostic_item_id']==published['diagnostic_item_id'] for p in after['diagnostic_prompt'])


@pytest.mark.parametrize('fault',['withdraw','version'])
def test_prepare_then_start_race_rolls_back_original_placement(supplied,monkeypatch,fault):
    case=supplied
    turn=case['say']('race-'+fault)
    before=facts(case)
    original=StudyRepository.start_plan_item
    invoked=[]
    def changed(self, plan_item_id, key, *args, **kwargs):
        c=kwargs['connection']
        assert c.in_transaction
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]>0
        invoked.append(plan_item_id)
        if fault=='withdraw':
            c.execute("UPDATE specialized_supply_candidate SET status='rejected' WHERE candidate_id=?",(case['draft']['candidate_id'],))
        else:
            c.execute('UPDATE daily_plan SET version=version+1 WHERE session_id=?',(case['sid'],))
        return original(self,plan_item_id,key,*args,**kwargs)
    monkeypatch.setattr(StudyRepository,'start_plan_item',changed)
    result=execute(case,turn)
    assert invoked and result.status_code==409, (result.status_code,result.text)
    assert facts(case)==before
    with case['db'].connect() as c:
        assert c.execute('SELECT status FROM specialized_supply_candidate WHERE candidate_id=?',
            (case['draft']['candidate_id'],)).fetchone()[0]=='promoted'


def test_lost_receipt_restart_reads_old_start_and_new_voice_resumes_same_prompt(supplied,monkeypatch):
    case=supplied
    turn=case['say']('lost-start-receipt')
    dispatch=AgentCapabilityExecutor._dispatch
    def lost(self,step):
        value=dispatch(self,step)
        if step['agent_turn_id']==turn['agent_turn_id']:
            raise RuntimeError('isolated lost result after committed source-bound start')
        return value
    with monkeypatch.context() as patch:
        patch.setattr(AgentCapabilityExecutor,'_dispatch',lost)
        with pytest.raises(RuntimeError,match='isolated lost result'):
            execute(case,turn)
    persisted=facts(case)
    task=next(p for p in persisted['plan_item'] if p['source_ref']==case['published']['diagnostic_item_id'])
    prompt=next(p for p in persisted['diagnostic_prompt'] if p['diagnostic_item_id']==task['source_ref'])
    assert task['status']=='active' and task['started_at']
    with unclean_app(case, lane='supply_gap', headers=dict(case['client'].headers)) as client:
        paused=facts(case)
        assert next(p for p in paused['plan_item'] if p['plan_item_id']==task['plan_item_id'])['status']=='paused'
        calls=len(case['requests'])
        replay=client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert replay.status_code==200,replay.text
        assert replay.json()['execution']['result']['replayed']
        assert facts(case)==paused and len(case['requests'])==calls
        quote='继续刚才那项写作练习，保留原来的题'
        case['speech']['quote']=quote
        voice=capture(client,case['settings'],monkeypatch,case['sid'],quote,prompt,key='new-resume-voice')
        new=interpret(client,case['sid'],quote,'new-resume-command',voice)
        resumed=client.post(f"/v1/agent-turns/{new['agent_turn_id']}/execute")
        assert resumed.status_code==200,resumed.text
        after=facts(case)
        assert len(after['plan_item'])==len(persisted['plan_item'])
        assert after['daily_plan']==persisted['daily_plan']
        assert [p['prompt_id'] for p in after['diagnostic_prompt']]==[p['prompt_id'] for p in persisted['diagnostic_prompt']]
        assert next(p for p in after['plan_item'] if p['plan_item_id']==task['plan_item_id'])['status']=='active'
        events=[e for e in after['plan_item_event'] if e['plan_item_id']==task['plan_item_id']]
        assert sum(e['event_type']=='started' for e in events)==1
        assert sum(e['event_type']=='resumed' for e in events)==1


def test_three_unclean_restarts_resume_by_real_system_pause_journal(supplied, monkeypatch):
    case=supplied
    _, task, prompt=start(case)
    original=facts(case)
    with case['db'].connect() as c:
        prior_run=c.execute('SELECT run_id FROM runtime_state').fetchone()[0]
    runs={prior_run}
    # Leave old runtime contexts unclosed to reproduce unclean startup, not
    # end_runtime's separate clean-shutdown pause. No task/event is fabricated.
    with ExitStack() as stack:
        for index in range(3):
            client=stack.enter_context(unclean_app(case, lane='supply_gap', headers=dict(case['client'].headers)))
            paused=facts(case)
            events=[e for e in paused['plan_item_event'] if e['plan_item_id']==task['plan_item_id']]
            event=events[-1]
            payload=json.loads(event['payload_json'])
            with case['db'].connect() as c:
                current_run=c.execute('SELECT run_id FROM runtime_state').fetchone()[0]
                snapshot=TaskStartBinding.resume_snapshot(c,case['sid'],CHILD)
            assert current_run not in runs
            assert event['event_type']=='paused' and payload['source']=='system'
            assert payload['reason']=='unclean_restart'
            assert payload['runtime_run_id']==prior_run and payload['recovery_run_id']==current_run
            assert payload['uncheckpointed_duration_unknown'] is True
            assert snapshot['item']['pause_event_id']==event['event_id']
            assert snapshot['item']['goal_prompt_binding']['prompt_id']==prompt['prompt_id']
            quote='继续刚才那项写作练习，保留原来的题'
            case['speech']['quote']=quote
            voice=capture(client,case['settings'],monkeypatch,case['sid'],quote,prompt,key=f'restart-{index}-voice')
            turn=interpret(client,case['sid'],quote,f'restart-{index}-resume',voice)
            assert turn['runtime_capability']=='task.resume'
            resumed=client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
            assert resumed.status_code==200,resumed.text
            after=facts(case)
            assert after['daily_plan']==original['daily_plan']
            assert [p['plan_item_id'] for p in after['plan_item']]==[p['plan_item_id'] for p in original['plan_item']]
            assert [p['prompt_id'] for p in after['diagnostic_prompt']]==[p['prompt_id'] for p in original['diagnostic_prompt']]
            assert next(p for p in after['plan_item'] if p['plan_item_id']==task['plan_item_id'])['status']=='active'
            events=[e for e in after['plan_item_event'] if e['plan_item_id']==task['plan_item_id']]
            assert sum(e['event_type']=='started' for e in events)==1
            assert sum(e['event_type']=='resumed' for e in events)==index+1
            assert sum(e['event_type']=='paused' for e in events)==index+1
            count=len(case['requests'])
            replay=client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
            assert replay.status_code==200,replay.text
            assert facts(case)==after and len(case['requests'])==count
            runs.add(current_run)
            prior_run=current_run


def test_real_recovery_event_not_removed_placement_runtime_metadata_authorizes_resume(supplied):
    case=supplied
    _, task, _=start(case)
    with unclean_app(case, lane='supply_gap', headers=dict(case['client'].headers)):
        before=facts(case)
        with case['db'].connect() as c:
            c.execute("UPDATE plan_constraint_decision SET metadata_json=json_remove(metadata_json,'$.placement_runtime_id') WHERE json_extract(metadata_json,'$.plan_item_id')=?",(task['plan_item_id'],))
            c.commit()
            snapshot=TaskStartBinding.resume_snapshot(c,case['sid'],CHILD)
            assert snapshot['status']=='ready' and snapshot['item']['plan_item_id']==task['plan_item_id']
            event=c.execute('SELECT * FROM plan_item_event WHERE event_id=?',
                (snapshot['item']['pause_event_id'],)).fetchone()
            assert event['event_type']=='paused'
            assert json.loads(event['payload_json'])['reason']=='unclean_restart'
        after=facts(case)
        assert after['plan_item']==before['plan_item']
        assert after['plan_item_event']==before['plan_item_event']
        assert after['diagnostic_prompt']==before['diagnostic_prompt']
