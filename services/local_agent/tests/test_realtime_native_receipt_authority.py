"""Different HMAC operation/call read scopes; no duplicate execution authority."""
import asyncio
import pytest
from home_study_agent.realtime_capability_tools import RealtimeCapabilityTools,RealtimeCapabilityConflict
from home_study_agent.realtime_native_context import load_native_context
from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.repository import StudyRepository
from test_realtime_capability_tools import scenario as legacy_scenario,counts
from native_realtime_test_fixture import scenario
from test_ordinary_learning_real_source_execution import env,real_source


def test_new_call_semantic_replay_and_context_read_old_call_without_new_execute(scenario):
 client,db,gateway,root,old,_,initial,proposal,*_=scenario;sid=root.split('/')[-1]
 first,_=proposal('查一下本机学习资料',{'scope':'resources','track':'all'},reference=old['reference'],cap='system.inspect_learning')
 current=dict(first);phase=['proposal'];exec_calls=[]
 def exec_auth(c,sid,child,envelope,voice):
  return phase[0]=='proposal' and all(envelope.get(k)==current.get(k) for k in ('voice_input_id','supplier_session_id','input_item_id','response_id','call_id'))
 def read_auth(c,sid,child,envelope,voice):
  if phase[0]=='proposal':return envelope['voice_input_id']==current['voice_input_id']
  if phase[0]=='context':return voice['session_id']==sid
  return all(envelope.get(k)==current.get(k) for k in ('voice_input_id','supplier_session_id','input_item_id','response_id','call_id'))
 executor=AgentCapabilityExecutor(db,StudyRepository(db))
 def execute(turn):exec_calls.append(turn);return executor.execute(turn)
 service=RealtimeCapabilityTools(db,authorize=exec_auth,authorize_receipt=read_auth,execute_turn=execute)
 admitted=service.admit(sid,first)
 done=asyncio.run(service.execute(sid,admitted['action_id']))
 assert done['execution']['status']=='completed' and len(exec_calls)==1
 before=counts(db)
 current.update(response_id='new_provider_response',call_id='new_provider_call')
 replay=service.admit(sid,current)
 assert replay['action_id']==admitted['action_id'] and replay['proposal_status']=='replayed'
 assert replay['execution']['status']=='completed' and len(exec_calls)==1 and counts(db)==before
 # The same read proof cannot execute the original call on its old identity.
 with pytest.raises(RealtimeCapabilityConflict,match='OWNER_UNCONFIRMED'):asyncio.run(service.execute(sid,admitted['action_id']))
 phase[0]='context'
 restored=load_native_context(service,sid,authorize=lambda *a:True)
 assert restored['runtime_facts']['items'][0]['capability']=='system.inspect_learning'
 assert len(exec_calls)==1 and counts(db)==before


def test_receipt_authorizer_cannot_grant_admission_or_execution(scenario):
 client,db,gateway,root,old,_,initial,proposal,*_=scenario;sid=root.split('/')[-1]
 p,_=proposal('帮我复习初三地理第一课',{'subject':'地理'},reference=old['reference'])
 closed=RealtimeCapabilityTools(db,authorize=lambda *a:False,authorize_receipt=lambda *a:True)
 with pytest.raises(RealtimeCapabilityConflict,match='OWNER_UNCONFIRMED'):closed.admit(sid,p)
 good=RealtimeCapabilityTools(db,authorize=scenario[-1],authorize_receipt=lambda *a:True)
 saved=good.admit(sid,p)
 closed.execute_turn=lambda _:pytest.fail('Read permission must never execute a tool')
 with pytest.raises(RealtimeCapabilityConflict,match='OWNER_UNCONFIRMED'):asyncio.run(closed.execute(sid,saved['action_id']))
 assert closed.receipt(sid,saved['action_id'])['execution']['status']=='completed'
