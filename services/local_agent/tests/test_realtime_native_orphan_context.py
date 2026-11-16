"""Completed native input survives restart before any tool/reply, without replay."""
import json,time
import pytest
from home_study_agent.realtime_native_context import claim_mistake_workspace_input,load_native_context,record_native_input_witness
from home_study_agent.realtime_capability_tools import RealtimeCapabilityTools,RealtimeCapabilityConflict
from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
from home_study_agent.privacy_operation_receipt import generation
from test_realtime_capability_tools import scenario,counts
from test_ordinary_learning_real_source_execution import env,real_source


def admit_native(scenario,text,key,*,purpose='native_cloud_dialogue',recovery=False,witness=True):
 client,db,gateway,root,old,_,service,proposal,seen,authorize=scenario;sid=root.split('/')[-1]
 with db.connect() as c:before_gen=generation(c)
 captures=VoiceCaptureBindingService(db)
 captures.begin_realtime(sid,key,purpose,{'child_id':'child-primary-4',**({'local_recovery_only':True} if recovery else {})},
  model_version='PUBLIC_NATIVE_SYNTHETIC',captured_at_ms=int(time.time()*1000))
 result=captures.complete_realtime(sid,key,text)
 if witness:
  with db.connect() as c:
   c.execute('BEGIN IMMEDIATE');record_native_input_witness(service,c,sid,result['voice_input_id'],expected_generation=before_gen);c.commit()
 with db.connect() as c:canonical=c.execute('SELECT transcript FROM voice_input WHERE voice_input_id=?',(result['voice_input_id'],)).fetchone()['transcript']
 return {**result,'transcript':canonical}


def test_orphan_native_original_restores_before_first_tool_without_fabricating_reply_or_execution(scenario):
 client,db,gateway,root,old,_,service,proposal,seen,authorize=scenario;sid=root.split('/')[-1]
 voice=admit_native(scenario,'帮我复习初三地理第一课','native-orphan')
 before=counts(db);calls=[]
 reboot=RealtimeCapabilityTools(db,authorize=authorize,execute_turn=lambda tid:calls.append(tid))
 context=load_native_context(reboot,sid,authorize=lambda *a:True)
 assert context['recent_dialogue']==[{'role':'user','text':'帮我复习初三地理第一课','status':'unverified'}]
 assert context['pending_inputs'][0]['voice_input_id']==voice['voice_input_id']
 assert context['pending_inputs'][0]['client_action_id']=='native-orphan'
 assert context['pending_inputs'][0]['text']=='帮我复习初三地理第一课'
 assert context['pending_calls']==[] and context['runtime_facts']['items']==[]
 assert calls==[] and counts(db)==before
 assert 'function_call' not in json.dumps(context)


@pytest.mark.parametrize('kind,text', [
 ('capture','录入这道错题'), ('topics','打开高频题型'), ('topic_explain','讲讲这组错题'),
])
def test_mistake_workspace_claim_neither_replays_input_nor_consumes_other_child(scenario,kind,text):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 voice=admit_native(scenario,text,'native-workspace-claim')
 payload={'child_id':'child-primary-4','voice_input_id':voice['voice_input_id'],'kind':kind}
 with pytest.raises(ValueError):claim_mistake_workspace_input(db,sid,{**payload,'child_id':'another-child'})
 assert load_native_context(service,sid,authorize=lambda *a:True)['pending_inputs'][0]['voice_input_id']==voice['voice_input_id']
 assert claim_mistake_workspace_input(db,sid,payload)=={'claimed':True}
 assert claim_mistake_workspace_input(db,sid,payload)=={'claimed':False,'already_handled':True}
 assert load_native_context(service,sid,authorize=lambda *a:True)['pending_inputs']==[]


def test_only_current_generation_attested_native_purpose_can_restore(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 native=admit_native(scenario,'公开有凭证的新请求','native-valid')
 admit_native(scenario,'不能凭purpose字符串升级','native-no-witness',witness=False)
 admit_native(scenario,'普通旧转写不升级云历史','legacy-old',purpose='agent_command',witness=False)
 admit_native(scenario,'本机恢复原话不得出站','legacy-recovery',purpose='agent_command',recovery=True,witness=False)
 context=load_native_context(service,sid,authorize=lambda *a:True)
 assert [x['voice_input_id'] for x in context['pending_inputs']]==[native['voice_input_id']]
 data=json.dumps(context,ensure_ascii=False)
 for bad in ['不能凭purpose字符串升级','普通旧转写不升级云历史','本机恢复原话不得出站']:assert bad not in data
 with db.connect() as c:c.execute("UPDATE privacy_storage_generation SET generation='public-other-generation'");c.commit()
 assert load_native_context(service,sid,authorize=lambda *a:True)['pending_inputs']==[]


def test_recent_context_wins_bounded_budget_then_is_chronological(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 for index in range(3):
  admit_native(scenario,str(index)+'公开上下文'+('甲'*4990),'native-budget-'+str(index))
 context=load_native_context(service,sid,authorize=lambda *a:True)
 words=[x['text'] for x in context['recent_dialogue']]
 assert sum(map(len,words))<=12000 and len(words)==2
 assert words[0].startswith('1') and words[1].startswith('2')
 assert len(context['pending_inputs'])==2


def test_existing_native_reply_or_tool_is_not_an_orphan_and_revocation_denies_export(scenario):
 client,db,gateway,root,old,headers,service,proposal,*_=scenario;sid=root.split('/')[-1]
 voice=admit_native(scenario,'帮我复习初三地理第一课','native-has-tool')
 p,_=proposal('帮我复习初三地理第一课',{'subject':'地理','requested_grade':'初三'},voice=voice,key='native-tool')
 saved=service.admit(sid,p)
 context=load_native_context(service,sid,authorize=lambda *a:True)
 assert context['pending_inputs']==[] and context['runtime_facts']['items'][0]['request_saved']
 assert context['recent_dialogue'][0]['text']=='帮我复习初三地理第一课'
 revoked=client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':True,'cloud_audio_allowed':False})
 assert revoked.status_code==200
 with pytest.raises(RealtimeCapabilityConflict,match='CONSENT_REQUIRED'):load_native_context(service,sid,authorize=lambda *a:True)


def restored_dialogue(service,sid,p):
 body={key:p[key] for key in ('version','voice_input_id','supplier_session_id','input_item_id','response_id','input_source')}
 body.update(text='可以，我们先核对教材版本。',status='unverified',terminal='completed')
 return service.save_dialogue(sid,body)


def test_two_persisted_originals_keep_second_until_first_dialogue_is_saved(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 first=admit_native(scenario,'先复习地理','queue-first')
 second=admit_native(scenario,'然后复习数学','queue-second')
 before=load_native_context(service,sid,authorize=lambda *a:True)
 assert [x['voice_input_id'] for x in before['pending_inputs']]==[first['voice_input_id'],second['voice_input_id']]
 packet,_=proposal(first['transcript'],{'subject':'地理'},voice=first,key='queue-first-result')
 packet.update(input_source='persisted_native',supplier_session_id='restored_supplier')
 restored_dialogue(service,sid,packet)
 fresh=load_native_context(service,sid,authorize=lambda *a:True)
 assert [x['voice_input_id'] for x in fresh['pending_inputs']]==[second['voice_input_id']]
 reboot=RealtimeCapabilityTools(db,authorize=lambda *a:True,execute_turn=lambda _:pytest.fail('unexpected execution'))
 assert [x['voice_input_id'] for x in load_native_context(reboot,sid,authorize=lambda *a:True)['pending_inputs']]==[second['voice_input_id']]


def test_restoration_chain_allows_own_ack_and_multiple_tools_without_reexecution(scenario):
 client,db,gateway,root,old,_,service,proposal,seen,authorize=scenario;sid=root.split('/')[-1]
 voice=admit_native(scenario,'帮我复习初三地理第一课，人教版','restore-chain')
 first,_=proposal(voice['transcript'],{'subject':'地理','requested_grade':'初三'},voice=voice,key='restored-first')
 first.update(input_source='persisted_native',supplier_session_id='restored_supplier')
 restored_dialogue(service,sid,first)
 a=service.admit(sid,first)
 second,_=proposal(voice['transcript'],{'textbook_edition':'人教版'},voice=voice,key='restored-second',previous=a['action_id'])
 second.update(input_source='persisted_native',supplier_session_id='restored_supplier')
 b=service.admit(sid,second)
 assert a['execution']['status']==b['execution']['status']=='completed'
 assert b['public_result']['requested']['subject']=='地理'
 assert b['public_result']['requested']['textbook_edition']=='人教版'
 before=counts(db);calls=[]
 reboot=RealtimeCapabilityTools(db,authorize=authorize,execute_turn=lambda tid:calls.append(tid))
 assert reboot.admit(sid,second)['action_id']==b['action_id']
 assert reboot.receipt_for_identity(sid,second)['action_id']==b['action_id']
 assert calls==[] and counts(db)==before
 assert load_native_context(reboot,sid,authorize=lambda *a:True)['pending_inputs']==[]


def test_restoration_checks_consumption_inside_transaction_but_semantic_replay_is_read_only(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 voice=admit_native(scenario,'帮我复习地理第一课','restore-stale-tool')
 # A stale context may have advertised this orphan before the old connection
 # completed a tool. The final admission transaction must see that new row.
 assert load_native_context(service,sid,authorize=lambda *a:True)['pending_inputs']
 live,_=proposal(voice['transcript'],{'subject':'地理'},voice=voice,key='other-chain-tool')
 original=service.admit(sid,live)
 restored,_=proposal(voice['transcript'],{'subject':'地理'},voice=voice,key='restore-read')
 restored.update(input_source='persisted_native',supplier_session_id='new_supplier')
 before=counts(db)
 assert service.admit(sid,restored)['action_id']==original['action_id']
 assert counts(db)==before
 restored.update(args={'lesson':'第一课'},call_id='call_restore_new')
 with pytest.raises(RealtimeCapabilityConflict,match='RESTORED_SOURCE_ALREADY_CONSUMED'):service.admit(sid,restored)
 assert counts(db)==before
 # A native reply from another chain consumes an orphan too; the current
 # restoration chain's own acknowledgement was explicitly allowed above.
 other=admit_native(scenario,'请帮我复习数学','restore-stale-reply')
 oldp,_=proposal(other['transcript'],{'subject':'数学'},voice=other,key='other-chain-reply')
 body={key:oldp[key] for key in ('version','voice_input_id','supplier_session_id','input_item_id','response_id')}
 body.update(text='我们先核对数学教材。',status='unverified',terminal='completed')
 service.save_dialogue(sid,body)
 newp,_=proposal(other['transcript'],{'subject':'数学'},voice=other,key='restore-after-reply')
 newp.update(input_source='persisted_native',supplier_session_id='new_supplier')
 before=counts(db)
 with pytest.raises(RealtimeCapabilityConflict,match='RESTORED_SOURCE_ALREADY_CONSUMED'):service.admit(sid,newp)
 assert counts(db)==before


def test_restoration_requires_native_witness_and_rejects_local_recovery_or_unknown_source(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 for purpose,recovery,error in [('native_cloud_dialogue',False,'RESTORED_SOURCE_INVALID'),('agent_command',False,'RESTORED_SOURCE_INVALID'),('native_cloud_dialogue',True,'INPUT_LOCAL_ONLY')]:
  voice=admit_native(scenario,'公开合成恢复请求','restore-invalid-'+purpose+str(recovery),purpose=purpose,recovery=recovery,witness=False)
  p,_=proposal(voice['transcript'],{'subject':'地理'},voice=voice,key='invalid-'+purpose+str(recovery))
  p['input_source']='persisted_native';before=counts(db)
  with pytest.raises(RealtimeCapabilityConflict,match=error):service.admit(sid,p)
  assert counts(db)==before
 p['input_source']='model_claimed';before=counts(db)
 with pytest.raises(RealtimeCapabilityConflict,match='INPUT_SOURCE_INVALID'):service.admit(sid,p)
 assert counts(db)==before
