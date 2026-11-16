import json
import pytest
from home_study_agent.child_safety import ChildSafetyService
from home_study_agent.database import Database
from test_native_runtime_hmac_integration import native,admit,proposal,success,counts,CHILD
from test_ordinary_learning_real_source_execution import env


def test_actual_hmac_safety_is_atomic_idempotent_and_cannot_restore_or_execute(native):
 client,db,gateway,sid,parent,request,calls,faults=native
 words='公开合成安全测试：我现在有危险。'
 voice,body=admit(native,words=words)
 control=voice['local_control']
 assert set(control)=={'kind','intent','response_text','agent_turn_id','safety_event_id'}
 assert control['kind']=='child_safety' and control['intent']=='urgent_safety_support'
 assert control['agent_turn_id'] and control['safety_event_id']
 assert '停止后续云交互' in control['response_text']
 assert '不会发送' not in control['response_text'] and '不会把' not in control['response_text']
 with Database(db.path).connect() as c:
  original=c.execute('SELECT * FROM voice_input WHERE voice_input_id=?',(voice['voice_input_id'],)).fetchone()
  assert original['purpose']=='native_cloud_dialogue' and original['transcript']==words
  turn=c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?',(control['agent_turn_id'],)).fetchone()
  action=json.loads(turn['action_json'])
  assert turn['intent']=='urgent_safety_support' and action['source']=='native_child_safety_v1'
  assert action['local_only'] is True and action['cloud_transmission_allowed'] is False
  assert action['mastery_update_allowed'] is False and action['independent_evidence'] is False
  flag=json.loads(c.execute('SELECT summary_json FROM audit_event WHERE audit_id=?',('native-safety-'+voice['voice_input_id'],)).fetchone()[0])
  assert flag['agent_turn_id']==control['agent_turn_id'] and flag['safety_event_id']==control['safety_event_id']
  assert words not in json.dumps(flag,ensure_ascii=False)
 before=counts(db)
 assert success(request('input',body))==voice
 assert counts(db)==before
 alerts=ChildSafetyService(db).list_events(CHILD)
 assert len(alerts)==1 and alerts[0]['risk_level']=='R3' and words not in json.dumps(alerts,ensure_ascii=False)
 context=success(request('context'))
 assert context['recent_dialogue']==[] and context['pending_inputs']==[] and context['pending_calls']==[]
 assert context['runtime_facts']['items']==[] and control['safety_event_id'] not in json.dumps(context)
 p=proposal(voice,'safety-no-tool')
 rejected=success(request('proposal',p))
 assert rejected['not_admitted'] is True and rejected['execution']['status']=='failed'
 assert 'local_control' not in json.dumps(rejected) and control['safety_event_id'] not in json.dumps(rejected)
 restored={**p,'call_id':'safety-no-restored-tool','input_source':'persisted_native'}
 assert success(request('proposal',restored))['not_admitted'] is True
 dialogue={k:p[k] for k in ('version','voice_input_id','supplier_session_id','input_item_id','response_id')}
 dialogue.update(text='不应接纳的普通云回复',status='unverified',terminal='completed')
 assert request('dialogue',dialogue).status_code==409
 with Database(db.path).connect() as c:
  protected=dict(c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?',(control['agent_turn_id'],)).fetchone())
 forged={'final_response':'伪造覆盖安全支持','tool_status':'completed','result':{'mastery':True}}
 for suffix,method in [('/execute',client.post),('/outcome',client.put)]:
  response=method('/v1/agent-turns/'+control['agent_turn_id']+suffix,**({'json':forged} if suffix=='/outcome' else {}))
  assert response.status_code==409 and response.json()['detail']['code']=='NATIVE_DIALOGUE_NOT_EXECUTABLE'
 with Database(db.path).connect() as c:
  assert dict(c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?',(control['agent_turn_id'],)).fetchone())==protected
 assert counts(db)==before and calls==[] and gateway.requests==[]


@pytest.mark.parametrize('words,intent,risk',[('公开合成：我很难过。','emotional_distress_support','R1'),('公开合成：有人欺负我。','high_risk_child_safety','R2')])
def test_existing_r1_r2_policy_creates_real_private_reminder(native,words,intent,risk):
 voice,_=admit(native,key='risk-'+risk,words=words)
 assert voice['local_control']['intent']==intent
 alerts=ChildSafetyService(native[1]).list_events(CHILD)
 assert len(alerts)==1 and alerts[0]['risk_level']==risk
 assert words not in json.dumps(alerts,ensure_ascii=False)


def test_normal_geography_and_r0_or_quoted_lesson_remain_native_without_safety_alert(native):
 client,db,gateway,sid,parent,request,calls,faults=native
 for key,words in [('geo','帮我复习初三地理第一课'),('pressure','数学作业今天很累'),('quotation','课文里说有人不想活')]:
  voice,_=admit(native,key=key,words=words)
  assert 'local_control' not in voice
  if key=='geo':assert success(request('proposal',proposal(voice,'normal-geo')))['public_result']['request_saved']
 assert ChildSafetyService(db).list_events(CHILD)==[]
 assert counts(db)['model_call']==0 and gateway.requests==[]


def test_safety_event_failure_rolls_back_completed_voice_turn_and_flag(native,monkeypatch):
 import home_study_agent.realtime_native_safety as safety
 client,db,gateway,sid,parent,request,calls,faults=native
 def fail(*args,**kwargs):raise RuntimeError('PUBLIC_SYNTHETIC_EVENT_WRITE_FAILURE')
 monkeypatch.setattr(safety,'persist_safety_event',fail)
 before=counts(db)
 with pytest.raises(RuntimeError,match='PUBLIC_SYNTHETIC_EVENT_WRITE_FAILURE'):admit(native,key='atomic-risk',words='公开合成：我现在有危险。')
 after=counts(db)
 assert after['voice_input']==before['voice_input'] and after['agent_turn']==before['agent_turn']
 assert after['agent_plan']==before['agent_plan'] and after['model_call']==before['model_call']
 assert ChildSafetyService(db).list_events(CHILD)==[]
 with Database(db.path).connect() as c:assert c.execute("SELECT count(*) FROM audit_event WHERE actor='native_child_safety_v1'").fetchone()[0]==0
