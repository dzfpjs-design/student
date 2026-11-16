"""Native concrete timer tools use original transactions, no phrase classifier."""
import asyncio,json
import pytest
from home_study_agent.realtime_capability_tools import RealtimeCapabilityTools
from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.agent_runtime import AgentRuntimeStore
from test_realtime_capability_tools import scenario,counts
from test_ordinary_learning_real_source_execution import env,real_source
from test_content_free_voice_control_http import task_fixture


def active(scenario):
 client,db,gateway,root,old,headers,service,proposal,seen,authorize=scenario;sid=root.split('/')[-1]
 with db.connect() as c:
  c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,));c.commit()
 repo=task_fixture(db,sid)
 service.execute_turn=AgentCapabilityExecutor(db,repo).execute
 return sid,repo


def test_native_semantic_pause_hits_original_task_transaction_and_replay_is_readonly(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario
 sid,repo=active(scenario);before=counts(db)
 p,_=proposal('手头这段先留在这里，我想腾出手来',{},reference=old['reference'],cap='task.pause')
 receipt=service.admit(sid,p)
 result=asyncio.run(service.execute(sid,receipt['action_id']))
 assert result['execution']['status']=='completed',result
 assert repo._get_plan_item_by_id('control-task-1')['status']=='paused'
 assert repo._get_plan_item_by_id('control-task-2')['status']=='pending'
 assert result['public_result']['task_state']=='paused'
 with db.connect() as c:events=c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='paused'").fetchone()[0]
 reboot=RealtimeCapabilityTools(db,authorize=scenario[-1],execute_turn=service.execute_turn)
 assert reboot.admit(sid,p)['action_id']==receipt['action_id']
 assert reboot.receipt_for_identity(sid,p)['execution']['status']=='completed'
 with db.connect() as c:assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='paused'").fetchone()[0]==events==1
 assert counts(db)['model_call']==before['model_call'] and counts(db)['agent_outcome']==0


def test_native_break_and_resume_keep_original_task_with_real_transition_receipts(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario
 sid,repo=active(scenario);before=counts(db)
 p,_=proposal('脑子有点累，让这一段缓一缓吧',{},reference=old['reference'],cap='break.begin')
 a=service.admit(sid,p);r=asyncio.run(service.execute(sid,a['action_id']))
 assert r['execution']['status']=='completed' and r['public_result']['session_state']=='break',r
 assert repo.get_session(sid)['state']=='break' and repo._get_plan_item_by_id('control-task-1')['status']=='paused'
 q,_=proposal('精神回来了，接着手头原来那件事往下走',{},key='resume',cap='break.resume')
 b=service.admit(sid,q);r=asyncio.run(service.execute(sid,b['action_id']))
 assert r['execution']['status']=='completed' and r['public_result']['session_state']=='study',r
 assert repo.get_session(sid)['state']=='study' and repo._get_plan_item_by_id('control-task-1')['status']=='active'
 assert repo._get_plan_item_by_id('control-task-2')['status']=='pending'
 with db.connect() as c:assert c.execute('SELECT count(*) FROM break_control_receipt').fetchone()[0]==2
 assert counts(db)['model_call']==before['model_call'] and counts(db)['agent_outcome']==0


def test_replaced_active_task_is_not_retargeted_after_admission(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario
 sid,repo=active(scenario)
 p,_=proposal('这件先放一下',{},reference=old['reference'],cap='task.pause')
 receipt=service.admit(sid,p)
 repo.pause_plan_item('control-task-1','different-public-pause')
 repo.start_plan_item('control-task-2','different-public-start')
 result=asyncio.run(service.execute(sid,receipt['action_id']))
 assert result['execution']['status']!='completed'
 assert repo._get_plan_item_by_id('control-task-2')['status']=='active'


def test_consent_rechecked_in_original_repository_write(scenario,monkeypatch):
 client,db,gateway,root,old,headers,service,proposal,*_=scenario
 sid,repo=active(scenario)
 p,_=proposal('把手上的计时先留住',{},reference=old['reference'],cap='task.pause')
 receipt=service.admit(sid,p);original=repo.pause_plan_item
 def revoke_then_write(*args,**kwargs):
  revoked=client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':True,'cloud_audio_allowed':False})
  assert revoked.status_code==200
  return original(*args,**kwargs)
 monkeypatch.setattr(repo,'pause_plan_item',revoke_then_write)
 result=asyncio.run(service.execute(sid,receipt['action_id']))
 assert result['execution']['status']!='completed'
 assert repo._get_plan_item_by_id('control-task-1')['status']=='active'
 assert result['public_projection']['eligible'] is False


def test_legacy_turns_still_use_original_voice_inspector(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario
 with db.connect() as c:
  legacy=c.execute("SELECT agent_turn_id FROM agent_turn WHERE intent!='qwen_native_dialogue_unverified' LIMIT 1").fetchone()[0]
  assert AgentRuntimeStore.inspect_voice_control_execution(c,legacy,allow_planned=True) is None


def test_missing_native_target_manifest_cannot_fall_back_to_unbound_legacy_execution(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario
 sid,repo=active(scenario)
 p,_=proposal('这件先留在这里',{},reference=old['reference'],cap='task.pause')
 receipt=service.admit(sid,p)
 with db.connect() as c:
  c.execute("UPDATE agent_plan SET context_manifest_json=json_remove(context_manifest_json,'$.native_runtime_authority_v1') WHERE agent_turn_id=?",(receipt['agent_turn_id'],))
  c.commit()
 result=asyncio.run(service.execute(sid,receipt['action_id']))
 assert result['execution']['status']!='completed'
 assert repo._get_plan_item_by_id('control-task-1')['status']=='active'


def test_public_catalog_keeps_phase_tools_and_exact_enum_schemas_for_one_native_connection(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario
 sid,repo=active(scenario)
 catalog={x['name']:x for x in service.catalog(sid)['capabilities']}
 assert {'task.pause','break.begin','break.resume'}<=set(catalog)
 assert catalog['break.resume']['allowed_states']==['break']
 assert catalog['system.inspect_learning']['arguments_schema']['properties']['scope']['enum']==['resources','plan','memory','progress','requests','all']
 assert set(catalog['system.inspect_learning']['arguments_schema']['properties']['track']['enum'])=={'school','pet','competition_math','all'}
 assert catalog['task.pause']['arguments_schema']['additionalProperties'] is False
 # Being public/discoverable is not a grant to execute a wrong-stage action.
 p,_=proposal('继续吧',{},reference=old['reference'],cap='break.resume')
 with pytest.raises(ValueError,match='unavailable'):service.admit(sid,p)
