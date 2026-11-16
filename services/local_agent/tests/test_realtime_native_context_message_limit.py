import json
from home_study_agent.realtime_native_context import load_native_context
from test_realtime_native_orphan_context import admit_native
from test_realtime_capability_tools import scenario,counts
from test_ordinary_learning_real_source_execution import env,real_source


def test_short_replies_tools_and_orphans_share_latest_twenty_four_message_budget(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 for index in range(12):
  text='公开短输入'+str(index).zfill(2)
  voice=admit_native(scenario,text,'short-reply-'+str(index))
  p,_=proposal(text,{},voice=voice,key='short-reply-'+str(index))
  body={k:p[k] for k in ('version','voice_input_id','supplier_session_id','input_item_id','response_id')}
  body.update(text='公开短回复'+str(index).zfill(2),status='unverified',terminal='completed')
  service.save_dialogue(sid,body)
 toolvoice=admit_native(scenario,'公开请求复习地理','short-tool')
 p,_=proposal('公开请求复习地理',{'subject':'地理'},voice=toolvoice,key='short-tool')
 service.admit(sid,p)
 orphan=admit_native(scenario,'最新公开尚未处理原话','short-orphan')
 before=counts(db);result=load_native_context(service,sid,authorize=lambda *a:True)
 # Twelve user/reply pairs plus tool input and orphan used to produce26,
 # rejected by the actual FE's global24-message contract despite a tiny text size.
 messages=result['recent_dialogue']
 assert len(messages)==24
 assert sum(len(m['text']) for m in messages)<=12000
 assert messages[0]['text']=='公开短输入01'
 assert messages[-1]=={'role':'user','text':'最新公开尚未处理原话','status':'unverified'}
 assert [p['voice_input_id'] for p in result['pending_inputs']]==[orphan['voice_input_id']]
 assert result['runtime_facts']['items'][0]['request_saved']
 assert '公开短输入00' not in json.dumps(messages,ensure_ascii=False)
 assert counts(db)==before
