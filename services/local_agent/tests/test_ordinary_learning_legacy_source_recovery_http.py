"""Upgrade boundary: old real saved lessons lack only new proof side effects.
All questions/plans/Tutor/answers use original HTTP and ModelGateway mock transport.
No test writes/deletes/injects SQL authorization or mints a public/source proof.
"""
import asyncio
from datetime import datetime, timezone
import json
import pytest
from fastapi.testclient import TestClient
import test_ordinary_learning_real_source_execution as original
from test_ordinary_learning_real_source_execution import env, capture_text, interpret_voice, inspect_current, execute_save_replay

NEW_QUESTION='请给我讲解分数的分子和分母有什么区别。'
NEW_CHECK='分子和分母分别表示什么？'
NEW_NARRATION='分母说明平均分成的份数，分子说明取用的份数。'+NEW_CHECK
NEW_ANSWER='我的答案是分母表示平均分成的份数，分子表示取用了几份。'
OLD_SUMMARY='理解正弦和余弦的区别'

class LegacyRecoveryGateway(original.OriginalGatewayWithControlledTransport):
    def __init__(self,settings):
        super().__init__(settings)
        self.lesson='old';self.plan_mode='new';self.plan_question=original.QUESTION;self.plan_summary=OLD_SUMMARY
        self.plan_quotes=[]
    def transport(self,request):
        kind=self.kind.get()
        if kind=='agent_plan':
            self.transports.append(kind);self.plan_quotes.append(self.control.quote)
            assert request.url.host=='synthetic.invalid'
            content=json.dumps({'utterance_binding':{'role':'question','quote':self.control.quote},'objective':{'mode':self.plan_mode,'type':'learning','summary':self.plan_summary,'success_criteria':['能用自己的话区分当前概念'],'phase':'study'},'next_step':{'capability':'learning.explain','arguments':{'question':self.plan_question,'subject':'数学','support_level':6},'reason':'按本轮公开合成问题提出讲解','expected_evidence':['independent_response']},'spoken_transition':'我们核对本轮的问题。','requires_child_reply':True,'confidence':0.94},ensure_ascii=False)
        elif kind.startswith('tutor_hint_level_') and self.lesson=='new':
            self.transports.append(kind)
            content=json.dumps({'blackboard':{'title':'分子和分母','segments':[{'narration':NEW_NARRATION,'blocks':[{'kind':'text','text':'平均分成几份，取用了几份。'},{'kind':'question','text':NEW_CHECK}]}]}},ensure_ascii=False)
        elif kind=='learning_outcome_evaluation' and self.lesson=='new':
            self.transports.append(kind);body=json.loads(request.read());payload=json.loads(body['messages'][-1]['content'])
            assert set(payload)=={'question_asked','child_response','subject'}
            self.evaluation_payloads.append(payload)
            content=json.dumps({'understanding':'partial','evidence_summary':'本轮合成回答说明了份数的对应关系。','spoken_feedback':'我们再用一个不同的份数检查。','next_check':'把一张纸平均分成四份，取一份时分子与分母分别是多少？','confidence':0.8},ensure_ascii=False)
        else:
            return super().transport(request)
        import httpx
        return httpx.Response(200,request=request,json={'choices':[{'finish_reason':'stop','message':{'content':content}}],'usage':{'prompt_tokens':30,'completion_tokens':40}})


def legacy_rows(db,turn,tutor):
    with db.connect() as c:
        saved=c.execute('SELECT action_json,transcript,response_text FROM agent_turn WHERE agent_turn_id=?',(turn['agent_turn_id'],)).fetchone()
        model=c.execute('SELECT context_manifest_json FROM model_call WHERE call_id=?',(tutor['output_envelope']['request_id'],)).fetchone()
        return tuple(saved),model['context_manifest_json']

@pytest.fixture
def legacy_source(env,tmp_path,monkeypatch):
    import home_study_agent.ordinary_learning_text_projection as projection
    generator=None
    # Simulate the actual old version's absent new side effects, not a SQL policy rewrite.
    with monkeypatch.context() as older:
        older.setattr(original,'OriginalGatewayWithControlledTransport',LegacyRecoveryGateway)
        older.setattr(projection,'mint_input_policy',lambda *args,**kwargs:None)
        older.setattr(projection,'record_tutor_source',lambda *args,**kwargs:None)
        generator=original.real_source.__wrapped__(env,tmp_path,monkeypatch)
        values=next(generator)
    client,db,gateway,root,source,turn,tutor,headers=values
    before=legacy_rows(db,turn,tutor)
    assert projection.INPUT_KEY not in json.loads(before[0][0])
    assert projection.TUTOR_KEY not in json.loads(before[1])
    try:
        yield values,before
    finally:
        assert legacy_rows(db,turn,tutor)==before
        with pytest.raises(StopIteration):next(generator)


def open_new_lesson(values,question,key,*,mode='new',summary=None,proposal_question=None,allow_early_rejection=False):
    client,db,gateway,root,old,old_turn,old_tutor,headers=values
    gateway.lesson='new';gateway.plan_mode=mode;gateway.plan_question=proposal_question or question
    gateway.plan_summary=summary or question
    voice=capture_text(client,root,old['reference'],key,question)
    assert voice['source_kind']=='realtime_transcript'
    turn=interpret_voice(client,root,gateway,voice,question,'question')
    if allow_early_rejection and turn.get('runtime_capability')!='learning.explain':
        assert gateway.plan_quotes[-1]==question
        assert turn['action']['type']=='none' and turn['action'].get('local_only') is True
        assert turn['action'].get('cloud_transmission_allowed') is False
        assert gateway.evaluation_payloads==[]
        return turn,voice,None,None,None,None
    assert turn['runtime_capability']=='learning.explain',(turn['intent'],turn['action'].get('source'))
    assert turn['action']['voice_input_binding']['question_reference']==old['reference']
    with db.connect() as c:
        row=c.execute('''SELECT p.plan_id,p.plan_json,p.objective_id,p.created_at plan_created,o.created_at goal_created,o.summary,o.last_agent_turn_id,
            (SELECT MIN(p2.created_at)FROM agent_plan p2 WHERE p2.objective_id=p.objective_id)first_plan_created
            FROM agent_plan p JOIN agent_objective o USING(objective_id) WHERE p.agent_turn_id=?''',(turn['agent_turn_id'],)).fetchone()
        plan=json.loads(row['plan_json']);plan_info=dict(row)
    action=turn['action']
    taught=client.post(root+'/tutor',json={'subject':action['subject'],'question':action['question'],'hint_level':action['hint_level'],'client_action_id':key+'-tutor'})
    assert taught.status_code==200,taught.text
    tutor=taught.json()
    output={k:tutor[k]for k in ('provider','model','response_kind','verification_status','safety_status')};output.update(tutor_turn_id=tutor['turn_id'],blackboard_present=True)
    receipt={'capability':'learning.explain','execution_location':'local_model','action_type':'ask_tutor','status':'completed','completed_at':datetime.now(timezone.utc).isoformat(),'output':output}
    completed=client.put('/v1/agent-turns/'+turn['agent_turn_id']+'/outcome',json={'final_response':tutor['answer'],'tool_status':'completed','result':{'execution_receipt':receipt}})
    assert completed.status_code==200,completed.text
    state=client.get(root+'/agent-runtime');assert state.status_code==200,state.text
    source=state.json()['pending_learning_capture'];assert source is not None
    assert source['reference']['binding']['source_agent_turn_id']==turn['agent_turn_id']
    return turn,voice,tutor,source,plan,plan_info


def test_legacy_unproved_source_cannot_trap_a_genuine_new_voice_question(legacy_source):
    values,old_rows=legacy_source;client,db,gateway,root,old,old_turn,old_tutor,headers=values
    turn,voice,tutor,source,plan,info=open_new_lesson(values,NEW_QUESTION,'legacy-genuine-new-question')
    assert plan['objective']['mode']=='new'
    assert info['objective_id']!=old['reference']['binding']['objective_id']
    assert info['goal_created']==info['plan_created']==info['first_plan_created']
    assert info['last_agent_turn_id']==turn['agent_turn_id']
    assert turn['action']['question']==NEW_QUESTION
    assert plan['next_step']['arguments']['question']==NEW_QUESTION
    assert info['summary']!=OLD_SUMMARY
    answer=capture_text(client,root,source['reference'],'legacy-new-lesson-answer',NEW_ANSWER)
    evaluated=interpret_voice(client,root,gateway,answer,NEW_ANSWER)
    inspect_current(db,evaluated,answer)
    execute_save_replay(client,db,gateway,evaluated,NEW_ANSWER,NEW_NARRATION)
    assert legacy_rows(db,old_turn,old_tutor)==old_rows

def test_same_goal_derived_reexplain_cannot_launder_a_legacy_unproved_lesson(legacy_source):
    values,old_rows=legacy_source;client,db,gateway,root,old,old_turn,old_tutor,headers=values
    question='刚才那题我还没听懂，请再讲讲。'
    gateway.lesson='new';gateway.plan_mode='continue';gateway.plan_question=question;gateway.plan_summary=OLD_SUMMARY
    voice=capture_text(client,root,old['reference'],'legacy-derived-reexplain-onset',question)
    turn=interpret_voice(client,root,gateway,voice,question,'question')
    if turn.get('runtime_capability')!='learning.explain':
        # A derived instruction may be rejected at the original admission gate before it becomes a lesson.
        assert turn['action']['type']=='none' and turn['action'].get('local_only') is True
        assert turn['action'].get('cloud_transmission_allowed') is False
        assert gateway.evaluation_payloads==[]
        assert legacy_rows(db,old_turn,old_tutor)==old_rows
        return
    with db.connect() as c:
        row=c.execute('SELECT p.objective_id,p.plan_json FROM agent_plan p WHERE p.agent_turn_id=?',(turn['agent_turn_id'],)).fetchone()
        assert row['objective_id']==old['reference']['binding']['objective_id']
        assert json.loads(row['plan_json'])['objective']['mode']=='continue'
    action=turn['action']
    taught=client.post(root+'/tutor',json={'subject':action['subject'],'question':action['question'],'hint_level':action['hint_level'],'client_action_id':'legacy-derived-tutor'})
    assert taught.status_code==200,taught.text
    tutor=taught.json();output={k:tutor[k]for k in ('provider','model','response_kind','verification_status','safety_status')};output.update(tutor_turn_id=tutor['turn_id'],blackboard_present=True)
    receipt={'capability':'learning.explain','execution_location':'local_model','action_type':'ask_tutor','status':'completed','completed_at':datetime.now(timezone.utc).isoformat(),'output':output}
    done=client.put('/v1/agent-turns/'+turn['agent_turn_id']+'/outcome',json={'final_response':tutor['answer'],'tool_status':'completed','result':{'execution_receipt':receipt}});assert done.status_code==200,done.text
    state=client.get(root+'/agent-runtime');assert state.status_code==200,state.text
    source=state.json()['pending_learning_capture'];assert source is not None
    answer=capture_text(client,root,source['reference'],'legacy-derived-evaluation-onset',NEW_ANSWER)
    attempted=interpret_voice(client,root,gateway,answer,NEW_ANSWER)
    assert attempted['runtime_capability']=='learning.evaluate_response'
    rejected=client.post('/v1/agent-turns/'+attempted['agent_turn_id']+'/execute')
    assert rejected.status_code==409,rejected.text
    assert gateway.evaluation_payloads==[]
    assert not any(p=='learning_outcome_evaluation'for p in gateway.transports)
    assert legacy_rows(db,old_turn,old_tutor)==old_rows
    with db.connect() as c:assert c.execute('SELECT COUNT(*)FROM agent_outcome').fetchone()[0]==0


def test_old_question_model_proposal_cannot_replace_the_new_original_voice_question(legacy_source,tmp_path):
    values,old_rows=legacy_source;client,db,gateway,root,old,old_turn,old_tutor,headers=values
    turn,voice,tutor,source,plan,info=open_new_lesson(values,NEW_QUESTION,'legacy-model-old-question',mode='new',summary=OLD_SUMMARY,proposal_question=OLD_SUMMARY,allow_early_rejection=True)
    if plan is None:
        (tmp_path/'finite-model-proposal-path.json').write_text(json.dumps({'trusted_original_reconstruction':False,'bad_model_question_persisted':False,'rejected_before_new_lesson':True},sort_keys=True))
        assert gateway.evaluation_payloads==[] and legacy_rows(db,old_turn,old_tutor)==old_rows
        return
    actual=plan['next_step']['arguments']['question']
    if actual==NEW_QUESTION:
        # The existing deterministic extraction may own this route, so the configured malicious model plan was never trusted.
        assert turn['action']['question']==NEW_QUESTION and plan['objective']['mode']=='new'
        assert info['objective_id']!=old['reference']['binding']['objective_id']
        assert info['last_agent_turn_id']==turn['agent_turn_id']
        (tmp_path/'finite-model-proposal-path.json').write_text(json.dumps({'trusted_original_reconstruction':True,'bad_model_question_persisted':False,'model_plan_requested_for_new_question':gateway.plan_quotes[-1]==NEW_QUESTION},sort_keys=True))
        assert gateway.evaluation_payloads==[]
    else:
        assert actual==OLD_SUMMARY and turn['action']['question']!=NEW_QUESTION
        assert plan['objective']['mode']=='new' and gateway.plan_quotes[-1]==NEW_QUESTION
        answer=capture_text(client,root,source['reference'],'legacy-model-old-question-answer',NEW_ANSWER)
        attempted=interpret_voice(client,root,gateway,answer,NEW_ANSWER)
        assert attempted['runtime_capability']=='learning.evaluate_response'
        rejected=client.post('/v1/agent-turns/'+attempted['agent_turn_id']+'/execute')
        assert rejected.status_code==409,rejected.text
        assert gateway.evaluation_payloads==[]
        assert not any(p=='learning_outcome_evaluation'for p in gateway.transports)
        (tmp_path/'finite-model-proposal-path.json').write_text(json.dumps({'trusted_original_reconstruction':False,'bad_model_question_persisted':True,'evaluator_dispatch_rejected':True},sort_keys=True))
    assert legacy_rows(db,old_turn,old_tutor)==old_rows


@pytest.fixture
def legacy_paused_source(legacy_source):
    values,before=legacy_source
    client,db,gateway,root,source,turn,tutor,headers=values
    begin=client.post(root+'/tutor-playback/begin',json={'turn_id':tutor['turn_id'],'client_action_id':'legacy-owned-playback-begin'})
    assert begin.status_code==200,begin.text
    playback=begin.json()['playback']
    binding={k:playback[k]for k in ('playback_id','source_version','generation')};binding['expected_revision']=playback['revision']
    paused=client.post(root+'/tutor-playback/control',json={**binding,'command':'pause','client_action_id':'legacy-owned-playback-pause'})
    assert paused.status_code==200,paused.text
    assert paused.json()['playback']['status']=='paused'
    # No started/ended event or heard/delivered claim is generated by this fixture.
    assert legacy_rows(db,turn,tutor)==before
    return legacy_source

def test_legacy_paused_saved_lesson_has_a_distinct_new_question_recovery_control(legacy_paused_source):
    test_legacy_unproved_source_cannot_trap_a_genuine_new_voice_question(legacy_paused_source)
