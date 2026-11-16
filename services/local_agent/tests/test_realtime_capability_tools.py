"""Actual Runtime/HTTP/SQLite sources, synthetic transport, no family/device/cloud."""
import asyncio,json
import pytest
from home_study_agent.realtime_capability_tools import RealtimeCapabilityTools,RealtimeCapabilityConflict,PREPARE,KEY
from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.repository import StudyRepository
from test_ordinary_learning_real_source_execution import env,real_source,capture_text

@pytest.fixture
def scenario(real_source):
 client,db,gateway,root,old,*tail=real_source;headers=tail[-1]
 consent=client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':True,'cloud_audio_allowed':True})
 assert consent.status_code==200,consent.text
 seen={}
 def authorize(c,sid,child,envelope,voice):
  return seen.get(envelope['input_item_id'])==voice['voice_input_id']
 service=RealtimeCapabilityTools(db,authorize=authorize)
 def proposal(words,args,*,reference=None,key='new',cap=PREPARE,voice=None,previous=None):
  if voice is None:voice=capture_text(client,root,reference,'public-native-'+key,words)
  item='item_'+key;seen[item]=voice['voice_input_id']
  p={'version':1,'voice_input_id':voice['voice_input_id'],'supplier_session_id':'public_supplier','input_item_id':item,
     'response_id':'response_'+key,'call_id':'call_'+key,'capability_name':cap,'args':args}
  if previous:p['previous_action_id']=previous
  return p,voice
 yield client,db,gateway,root,old,headers,service,proposal,seen,authorize


def counts(db):
 with db.connect() as c:
  return {t:c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['agent_turn','agent_plan','agent_objective','agent_outcome','model_call']}


def test_catalog_is_static_public_specs_not_owned_sources(scenario):
 client,db,gateway,root,old,_,service,*_=scenario
 cat=service.catalog(root.split('/')[-1]);encoded=json.dumps(cat,ensure_ascii=False)
 assert any(x['name']==PREPARE for x in cat['capabilities'])
 assert old['reference']['binding']['source_agent_turn_id'] not in encoded
 assert 'child-primary-4' not in encoded and 'voice_input_id' not in encoded
 assert len({x['tool_name'] for x in cat['capabilities']})==len(cat['capabilities'])
 assert all(len(x['tool_name'])<=64 for x in cat['capabilities'])


@pytest.mark.parametrize('words,args',[
 ('帮我复习初三地理第一课',{'subject':'地理','requested_grade':'初三','lesson':'第一课'}),
 ('请帮我复习三年级数学第一课',{'subject':'数学','requested_grade':'三年级','lesson':'第一课'}),
 ('请带我复习八年级物理第一章',{'subject':'物理','requested_grade':'八年级','lesson':'第一章'}),
])
def test_native_tool_saves_real_new_learning_goal_without_guessing_material_or_changing_profile(scenario,words,args):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 with db.connect() as c:before=[tuple(x) for x in c.execute('SELECT * FROM child_profile ORDER BY child_id')]
 prior=counts(db)
 p,_=proposal(words,args,reference=old['reference'])
 result=service.admit(sid,p)
 assert result['execution']['status']=='completed'
 facts=result['public_result']
 assert facts['request_saved'] is True and facts['requested']==args
 assert facts['material_status']=='needs_material_identity' and facts['missing_fields']==['textbook_edition','lesson_title']
 assert facts['source_verified'] is False and result['public_projection']['verified_model_text'] is False
 with db.connect() as c:
  after=[tuple(x) for x in c.execute('SELECT * FROM child_profile ORDER BY child_id')]
  goals=c.execute('SELECT summary,status FROM agent_objective ORDER BY rowid').fetchall()
 assert before==after and goals[-1]['summary']==words and goals[-1]['status']=='active'
 assert any(x['status']=='paused' for x in goals[:-1])
 assert counts(db)['agent_outcome']==prior['agent_outcome']==0
 assert counts(db)['model_call']==prior['model_call']


def test_followup_and_changed_subject_are_durable_goals_without_fabricated_curriculum(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 first,_=proposal('帮我复习初三地理第一课',{'subject':'地理','requested_grade':'初三','lesson':'第一课'},reference=old['reference'])
 a=service.admit(sid,first)
 follow,_=proposal('用人教版，课名是地球和地球仪',{'textbook_edition':'人教版','lesson_title':'地球和地球仪'},key='follow')
 b=service.admit(sid,follow)
 assert b['public_result']['requested']['subject']=='地理'
 assert b['public_result']['material_status']=='needs_source_verification'
 change,_=proposal('改为三年级数学第一课',{'subject':'数学','requested_grade':'三年级','lesson':'第一课'},key='change')
 z=service.admit(sid,change)
 assert z['public_result']['requested']['subject']=='数学'
 assert 'textbook_edition' not in z['public_result']['requested']
 with db.connect() as c:
  goals=c.execute('SELECT status FROM agent_objective ORDER BY rowid').fetchall()
 assert goals[-1]['status']=='active' and all(x['status']=='paused' for x in goals[:-1])
 assert counts(db)['agent_outcome']==0


def test_same_input_multiple_tools_idempotent_after_new_service_instance(scenario):
 client,db,gateway,root,old,_,service,proposal,seen,authorize=scenario;sid=root.split('/')[-1]
 words='帮我复习初三地理第一课，人教版，地球和地球仪'
 first,voice=proposal(words,{'subject':'地理','requested_grade':'初三','lesson':'第一课'},reference=old['reference'])
 a=service.admit(sid,first)
 second,_=proposal(words,{'textbook_edition':'人教版','lesson_title':'地球和地球仪'},voice=voice,key='second',previous=a['action_id'])
 b=service.admit(sid,second);before=counts(db)
 reboot=RealtimeCapabilityTools(db,authorize=authorize)
 replay=reboot.admit(sid,second)
 assert replay['action_id']==b['action_id'] and replay['proposal_status']=='replayed'
 assert replay['public_result']==b['public_result'] and counts(db)==before
 assert reboot.receipt_for_identity(sid,second)['action_id']==b['action_id']
 conflicting={**second,'args':{'subject':'数学'}}
 with pytest.raises(RealtimeCapabilityConflict,match='REPLAY_CONFLICT'):reboot.admit(sid,conflicting)


def test_input_binding_and_literal_true_authorization_are_required(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 p,_=proposal('帮我复习初三地理第一课',{'subject':'地理'},reference=old['reference'])
 for authorize in (None,lambda *a:1):
  closed=RealtimeCapabilityTools(db,authorize=authorize)
  with pytest.raises(RealtimeCapabilityConflict,match='OWNER_UNCONFIRMED'):closed.admit(sid,p)
 bad={**p,'input_item_id':'unbound_item'}
 with pytest.raises(RealtimeCapabilityConflict,match='OWNER_UNCONFIRMED'):service.admit(sid,bad)


def test_consent_denies_tools_but_native_dialogue_is_only_unverified_local_history(scenario):
 client,db,gateway,root,old,headers,service,proposal,*_=scenario;sid=root.split('/')[-1]
 p,_=proposal('帮我复习初三地理第一课',{'subject':'地理'},reference=old['reference'])
 revoked=client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':True,'cloud_audio_allowed':False})
 assert revoked.status_code==200
 with pytest.raises(RealtimeCapabilityConflict,match='CONSENT_REQUIRED'):service.admit(sid,p)
 body={k:p[k] for k in ['version','voice_input_id','supplier_session_id','input_item_id','response_id']}
 body.update(text='我理解你想复习地理，我们先核对教材版本。',status='unverified',terminal='completed')
 before=counts(db);saved=service.save_dialogue(sid,body)
 assert saved['verified'] is False
 replay=service.save_dialogue(sid,body);assert replay['agent_turn_id']==saved['agent_turn_id'] and replay['replayed']
 after=counts(db)
 assert after['agent_turn']==before['agent_turn']+1
 assert all(after[k]==before[k] for k in ['agent_plan','agent_objective','agent_outcome','model_call'])
 with db.connect() as c:
  action=json.loads(c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?',(saved['agent_turn_id'],)).fetchone()[0])
 assert action['mastery_update_allowed'] is action['independent_evidence'] is False
 assert action['cloud_transmission_allowed'] is False


def test_old_question_does_not_authorize_scoring_a_new_request(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 p,voice=proposal('帮我复习初三地理第一课',{'subject':'地理'},reference=old['reference'])
 a=service.admit(sid,p)
 score,_=proposal('帮我复习初三地理第一课',{'response':'帮我复习初三地理第一课','subject':'地理'},voice=voice,key='score',cap='learning.evaluate_response',previous=a['action_id'])
 with pytest.raises((RealtimeCapabilityConflict,ValueError),match='SCORING_AUTHORITY|SOURCE|REFERENCE|VOICE|binding'):service.admit(sid,score)
 assert counts(db)['agent_outcome']==0


def test_unknown_is_read_only_and_new_call_cannot_repeat_unresolved_effect(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 p,_=proposal('看看我的学习资料状态',{'scope':'resources','track':'all'},reference=old['reference'],cap='system.inspect_learning')
 first=service.admit(sid,p);calls=[]
 async def uncertain(turn):calls.append(turn);raise RuntimeError('public synthetic response loss')
 service.execute_turn=uncertain
 a=asyncio.run(service.execute(sid,first['action_id']))
 assert a['execution']['status']=='unknown' and len(calls)==1
 b=asyncio.run(service.execute(sid,first['action_id']))
 assert b['execution']['status']=='unknown' and len(calls)==1
 other={**p,'call_id':'different_call','args':{'scope':'plan','track':'all'},'previous_action_id':first['action_id']}
 with pytest.raises(RealtimeCapabilityConflict,match='OUTCOME_UNKNOWN'):service.admit(sid,other)


def test_real_registered_server_executor_saves_receipt_without_exposing_private_result(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 p,_=proposal('看看我的学习资料状态',{'scope':'resources','track':'all'},reference=old['reference'],cap='system.inspect_learning')
 first=service.admit(sid,p)
 executor=AgentCapabilityExecutor(db,StudyRepository(db))
 service.execute_turn=executor.execute
 result=asyncio.run(service.execute(sid,first['action_id']))
 assert result['execution']['status']=='completed',result
 assert result['public_result']['inventory_scope']=='local_index_metadata'
 assert result['public_result']['inventory']['ready_curriculum_resource_count']>=0
 assert result['public_result']['material_content_verified'] is False
 assert 'response_text' not in result['public_result'] and 'latest_daily_plan' not in result['public_result']
 assert counts(db)['agent_outcome']==0
 with db.connect() as c:
  saved=c.execute('SELECT result_json FROM agent_step JOIN agent_plan USING(plan_id) WHERE agent_turn_id=?',(first['agent_turn_id'],)).fetchone()[0]
 assert saved and 'response_text' in json.loads(saved)


def test_changed_tool_arguments_are_rejected_before_execution(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 p,_=proposal('看看我的学习资料状态',{'scope':'resources','track':'all'},reference=old['reference'],cap='system.inspect_learning')
 first=service.admit(sid,p)
 with db.connect() as c:
  c.execute("UPDATE agent_step SET arguments_json=? WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)",(json.dumps({'scope':'memory','track':'all'}),first['agent_turn_id']))
  c.commit()
 with db.connect() as c:
  with pytest.raises(RealtimeCapabilityConflict,match='TURN_CHANGED'):service.validate_turn(c,first['agent_turn_id'])


def test_semantic_grade_alias_and_metadata_paraphrase_do_not_reject_the_learning_request(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 p,_=proposal('帮我复习初三地理第一课',{'subject':'地理','requested_grade':'九年级','lesson':'第1课'},reference=old['reference'])
 result=service.admit(sid,p)
 assert result['execution']['status']=='completed' and result['public_result']['request_saved']
 assert result['public_result']['requested']['requested_grade']=='九年级'
 assert result['public_result']['requested']['lesson']=='第1课'
 assert result['public_result']['metadata_status']=='proposed'
 assert result['public_result']['source_verified'] is False

def test_receipt_remains_local_readable_but_facts_withheld_immediately_after_revocation(scenario):
 client,db,gateway,root,old,headers,service,proposal,*_=scenario;sid=root.split('/')[-1]
 p,_=proposal('帮我复习初三地理第一课',{'subject':'地理'},reference=old['reference'])
 result=service.admit(sid,p)
 assert result['public_projection']['eligible'] is True
 revoked=client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':False,'cloud_audio_allowed':False})
 assert revoked.status_code==200
 local=service.receipt(sid,result['action_id'])
 assert local['execution']['status']=='completed' and local['public_projection']['eligible'] is False
 assert local['public_result']=={'version':1,'withheld':True,'reason':'current_cloud_permission_or_owner_required'}
 assert service.admit(sid,p)['public_result']==local['public_result']


def test_confirmed_failure_does_not_lock_unrelated_new_requests(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 p,_=proposal('看看我的学习资料状态',{'scope':'resources','track':'all'},reference=old['reference'],cap='system.inspect_learning')
 first=service.admit(sid,p)
 def failed(turn_id):
  with db.connect() as c:
   c.execute("UPDATE agent_step SET status='failed',error_code='PUBLIC_SYNTHETIC_NO_EFFECT' WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)",(turn_id,))
   c.commit()
 service.execute_turn=failed
 failure=asyncio.run(service.execute(sid,first['action_id']))
 assert failure['execution']['status']=='failed'
 independent,_=proposal('帮我复习三年级数学第一课',{'subject':'数学','requested_grade':'三年级','lesson':'第一课'},key='independent')
 result=service.admit(sid,independent)
 assert result['execution']['status']=='completed' and result['public_result']['request_saved']

def test_unknown_same_input_is_held_but_new_learning_intake_can_be_saved(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 p,_=proposal('看看我的学习资料状态',{'scope':'resources','track':'all'},reference=old['reference'],cap='system.inspect_learning')
 first=service.admit(sid,p)
 async def uncertain(turn):raise RuntimeError('public fixture response loss')
 service.execute_turn=uncertain
 assert asyncio.run(service.execute(sid,first['action_id']))['execution']['status']=='unknown'
 independent,_=proposal('帮我复习初三地理第一课',{'subject':'地理'},key='independent')
 result=service.admit(sid,independent)
 assert result['execution']['status']=='completed' and result['public_result']['request_saved']


def test_native_execute_does_not_accept_an_old_legacy_turn(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 with db.connect() as c:legacy=c.execute("SELECT client_action_id FROM agent_turn WHERE orchestrator_version!=? LIMIT 1",('qwen_runtime_capability_v1',)).fetchone()[0]
 with pytest.raises(RealtimeCapabilityConflict,match='TURN_NOT_NATIVE'):asyncio.run(service.execute(sid,legacy))

def test_missing_native_manifest_is_a_denial_not_an_old_route_fallback(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 p,_=proposal('看看我的学习资料状态',{'scope':'resources','track':'all'},reference=old['reference'],cap='system.inspect_learning')
 first=service.admit(sid,p)
 with db.connect() as c:
  c.execute("UPDATE agent_plan SET context_manifest_json='{}' WHERE agent_turn_id=?",(first['agent_turn_id'],))
  c.commit()
 with db.connect() as c:
  with pytest.raises(RealtimeCapabilityConflict,match='TURN_CHANGED'):service.validate_turn(c,first['agent_turn_id'])
