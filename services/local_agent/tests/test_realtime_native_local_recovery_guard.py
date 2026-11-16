"""Real legacy recovery admission stays local after general cloud opt-in."""
import importlib.util,time
from pathlib import Path
import pytest
from home_study_agent.realtime_capability_tools import RealtimeCapabilityTools,RealtimeCapabilityConflict
from test_realtime_capability_tools import scenario,counts
from test_ordinary_learning_real_source_execution import env,real_source


def recovery(scenario):
 client,db,gateway,root,old,headers,service,proposal,seen,authorize=scenario
 started=client.post(root+'/voice/realtime/begin',json={'client_action_id':'public-native-recovery','model_label':'PUBLIC_SYNTHETIC_ASR',
  'captured_at_ms':int(time.time()*1000),'capture_reference':{'child_id':'child-primary-4','local_recovery_only':True}})
 assert started.status_code==200,started.text
 done=client.post(root+'/voice/realtime/complete',json={'client_action_id':'public-native-recovery','transcript':'仅在本机恢复的合成原话'})
 assert done.status_code==200,done.text
 assert done.json()['local_recovery_only'] is True
 p,_=proposal('仅在本机恢复的合成原话',{'subject':'地理'},voice=done.json(),key='recovery')
 return p


def test_cloud_consent_does_not_upgrade_local_recovery_voice_for_tool_or_native_dialogue(scenario):
 p=recovery(scenario);client,db,gateway,root,old,headers,service,proposal,*_=scenario;sid=root.split('/')[-1]
 before=counts(db)
 with pytest.raises(RealtimeCapabilityConflict,match='INPUT_LOCAL_ONLY'):service.admit(sid,p)
 dialogue={key:p[key] for key in ('version','voice_input_id','supplier_session_id','input_item_id','response_id')}
 dialogue.update(text='公开合成回复',status='unverified',terminal='completed')
 with pytest.raises(RealtimeCapabilityConflict,match='INPUT_LOCAL_ONLY'):service.save_dialogue(sid,dialogue)
 assert counts(db)==before


def test_pre_guard_bad_receipt_cannot_be_exported_after_the_source_guard(scenario):
 p=recovery(scenario);client,db,gateway,root,old,headers,service,proposal,seen,authorize=scenario;sid=root.split('/')[-1]
 # Reviewed frozen predecessor reproduces the old hole only in this tmp DB.
 source=Path.cwd()/'.agentic-engineering/operations/realtime-native-catalog-schema-source-E2-r1/services/local_agent/src/home_study_agent/realtime_capability_tools.py'
 spec=importlib.util.spec_from_file_location('home_study_agent.before_recovery_guard',source)
 previous=importlib.util.module_from_spec(spec);spec.loader.exec_module(previous)
 unsafe=previous.RealtimeCapabilityTools(db,authorize=authorize)
 original=unsafe.admit(sid,p)
 assert original['execution']['status']=='completed' # RED behavior of immutable predecessor.
 before=counts(db)
 with pytest.raises(RealtimeCapabilityConflict,match='INPUT_LOCAL_ONLY'):service.receipt(sid,original['action_id'])
 assert counts(db)==before
