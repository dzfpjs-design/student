"""Public model args expose authoritative enum values; no provider/side effects."""
import pytest
from home_study_agent.agent_runtime import CAPABILITY_ENUM_ARGUMENTS
from test_realtime_capability_tools import scenario,counts
from test_ordinary_learning_real_source_execution import env,real_source


def test_catalog_scope_track_schema_uses_actual_registered_choices(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario
 entries={x['name']:x for x in service.catalog(root.split('/')[-1])['capabilities']}
 assert {'learning.prepare_request','system.inspect_learning','task.pause','break.begin','break.resume'} <= set(entries)
 schema=entries['system.inspect_learning']['arguments_schema']
 assert schema['additionalProperties'] is False
 for name in ('scope','track'):
  assert schema['properties'][name]['type']=='string'
  assert schema['properties'][name]['enum']==list(CAPABILITY_ENUM_ARGUMENTS['system.inspect_learning'][name])
 assert set(schema['required'])=={'scope','track'}
 tutor=entries['learning.explain']['arguments_schema']['properties']
 assert tutor['question_relation']['enum']==['new','followup']
 assert 'source_turn_id' not in tutor and 'tutor_followup_binding' not in tutor


def test_invalid_semantic_argument_has_no_call_then_corrected_new_call_is_admitted(scenario):
 client,db,gateway,root,old,_,service,proposal,*_=scenario;sid=root.split('/')[-1]
 p,_=proposal('查询学习资料',{'scope':'not_an_actual_scope','track':'all'},reference=old['reference'],cap='system.inspect_learning')
 before=counts(db)
 with pytest.raises(ValueError,match='unsupported capability argument'):service.admit(sid,p)
 assert counts(db)==before
 fixed={**p,'call_id':'corrected_call','args':{'scope':'resources','track':'all'}}
 admitted=service.admit(sid,fixed)
 assert admitted['execution']['status']=='pending'
 assert counts(db)['agent_turn']==before['agent_turn']+1
