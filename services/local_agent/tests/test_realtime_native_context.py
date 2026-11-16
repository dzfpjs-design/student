"""Native-only restoration from actual Runtime/HTTP/SQLite; no provider call."""
import json
import pytest
from home_study_agent.realtime_capability_tools import RealtimeCapabilityTools,RealtimeCapabilityConflict
from home_study_agent.realtime_native_context import load_native_context
from test_realtime_capability_tools import scenario as legacy_scenario,counts
from native_realtime_test_fixture import scenario
from test_ordinary_learning_real_source_execution import env,real_source


def native_chat(service,sid,p,text='可以，我们先核对教材版本和课名。'):
 body={key:p[key] for key in ('version','voice_input_id','supplier_session_id','input_item_id','response_id')}
 body.update(text=text,status='unverified',terminal='completed')
 return service.save_dialogue(sid,body)


def test_restart_restores_only_admitted_native_words_unverified_reply_and_safe_runtime_facts(scenario):
 client,db,gateway,root,old,_,service,proposal,seen,authorize=scenario;sid=root.split('/')[-1]
 p,_=proposal('帮我复习初三地理第一课',{'subject':'地理','requested_grade':'初三','lesson':'第一课'},reference=old['reference'])
 first=service.admit(sid,p);native_chat(service,sid,p)
 before=counts(db)
 reboot=RealtimeCapabilityTools(db,authorize=authorize)
 context=load_native_context(reboot,sid,authorize=lambda *a:True)
 assert context['recent_dialogue']==[
  {'role':'user','text':'帮我复习初三地理第一课','status':'unverified'},
  {'role':'assistant','text':'可以，我们先核对教材版本和课名。','status':'unverified'}]
 facts=context['runtime_facts']['items']
 assert len(facts)==1 and facts[0]['request_saved'] and facts[0]['metadata_status']=='proposed'
 assert context['pending_calls']==[]
 serialized=json.dumps(context,ensure_ascii=False)
 for forbidden in ['正弦和余弦各自比较的是哪一条边与斜边','child-primary-4',old['reference']['binding']['source_agent_turn_id']]:assert forbidden not in serialized
 assert counts(db)==before
 assert load_native_context(reboot,sid,authorize=lambda *a:True)['revision']==context['revision']


def test_tool_input_is_restored_even_if_disconnect_happened_before_any_native_reply(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 p,_=proposal('想回顾一下初三地理开头那一课',{'subject':'地理','requested_grade':'九年级','lesson':'第一课'},reference=old['reference'])
 result=service.admit(sid,p)
 assert result['public_result']['requested']['lesson']=='第一课' # Native semantics need not quote independent ASR.
 assert result['public_result']['metadata_status']=='proposed' and result['public_result']['source_verified'] is False
 context=load_native_context(service,sid,authorize=lambda *a:True)
 assert context['recent_dialogue']==[{'role':'user','text':'想回顾一下初三地理开头那一课','status':'unverified'}]
 assert context['runtime_facts']['items'][0]['requested']['requested_grade']=='九年级'


def test_context_requires_fresh_permission_and_current_host_and_never_uses_legacy_history(scenario,legacy_scenario):
 client,db,gateway,root,old,headers,service,proposal,*_=scenario;sid=root.split('/')[-1]
 p,_=proposal('帮我复习初三地理第一课',{'subject':'地理'},reference=old['reference'])
 service.admit(sid,p);saved=native_chat(service,sid,p)
 for authorize in (None,lambda *a:1):
  with pytest.raises(RealtimeCapabilityConflict,match='HOST_UNCONFIRMED'):load_native_context(service,sid,authorize=authorize)
 # An arbitrary legacy response or modified native row never enters this projector.
 with db.connect() as c:
  c.execute('UPDATE agent_turn SET response_text=? WHERE agent_turn_id=?',('SECRET_SYNTHETIC_MUTATED_RESPONSE',saved['agent_turn_id']))
  c.commit()
 context=load_native_context(service,sid,authorize=lambda *a:True)
 assert 'SECRET_SYNTHETIC' not in json.dumps(context)
 # The original public legacy HTTP path is still deliberately ineligible.
 # Changing the positive fixture must not weaken this source exclusion.
 legacy,_=legacy_scenario[7]('LEGACY_SYNTHETIC_INPUT_MUST_STAY_LOCAL',{'subject':'地理'},key='legacy-negative')
 service.admit(sid,legacy);native_chat(service,sid,legacy,text='LEGACY_SYNTHETIC_REPLY_MUST_STAY_LOCAL')
 with db.connect() as c:
  row=c.execute('SELECT purpose FROM voice_input WHERE voice_input_id=?',(legacy['voice_input_id'],)).fetchone()
  assert row['purpose']=='agent_command'
  assert c.execute('SELECT 1 FROM audit_event WHERE audit_id=?',('native-input-'+legacy['voice_input_id'],)).fetchone() is None
 excluded=load_native_context(service,sid,authorize=lambda *a:True)
 assert 'LEGACY_SYNTHETIC' not in json.dumps(excluded)
 revoked=client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':False,'cloud_audio_allowed':False})
 assert revoked.status_code==200
 with pytest.raises(RealtimeCapabilityConflict,match='CONSENT_REQUIRED'):load_native_context(service,sid,authorize=lambda *a:True)


def test_other_session_native_content_and_pending_function_calls_are_not_replayed(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 p,_=proposal('查一下本机资料',{'scope':'resources','track':'all'},reference=old['reference'],cap='system.inspect_learning')
 result=service.admit(sid,p)
 context=load_native_context(service,sid,authorize=lambda *a:True)
 assert len(context['pending_calls'])==1
 assert context['pending_calls'][0]['call_id']==p['call_id'] and context['pending_calls'][0]['status']=='pending'
 assert context['runtime_facts']['items']==[]
 assert 'function_call' not in json.dumps(context)
 with db.connect() as c:other_child=c.execute("SELECT child_id FROM child_profile WHERE child_id!='child-primary-4' AND status='active' LIMIT 1").fetchone()[0]
 other=client.post(root+'/switch-child',json={'target_child_id':other_child,'client_action_id':'public-other-context'})
 assert other.status_code==200,other.text
 otherctx=load_native_context(service,other.json()['session_id'],authorize=lambda *a:True)
 assert otherctx['recent_dialogue']==[] and otherctx['runtime_facts']['items']==[] and otherctx['pending_calls']==[]


def test_reboot_context_pending_identity_reads_original_receipt_without_execution(scenario):
 client,db,gateway,root,old,_,service,proposal,seen,authorize=scenario;sid=root.split('/')[-1]
 p,_=proposal('查一下本机资料',{'scope':'resources','track':'all'},reference=old['reference'],cap='system.inspect_learning')
 first=service.admit(sid,p);before=counts(db);calls=[]
 reboot=RealtimeCapabilityTools(db,authorize=authorize,execute_turn=lambda turn:calls.append(turn))
 recovered=load_native_context(reboot,sid,authorize=lambda *a:True)
 pending=recovered['pending_calls'][0]
 assert pending['action_id']==first['action_id']
 assert set(pending['identity'])=={'version','voice_input_id','supplier_session_id','input_item_id','response_id','call_id'}
 assert 'host_connection_id' not in pending['identity']
 receipt=reboot.receipt_for_identity(sid,pending['identity'])
 assert receipt['action_id']==first['action_id'] and receipt['execution']['status']=='pending'
 assert counts(db)==before and calls==[]
 # Only dialogue/facts may be supplied as native model context. This private
 # recovery metadata never creates a function_call or a new capability request.
 model_context={key:recovered[key] for key in ('recent_dialogue','runtime_facts')}
 assert p['call_id'] not in json.dumps(model_context) and 'function_call' not in json.dumps(model_context)
