"""Public semantic tool schemas derived from the actual capability registry.

Private scope/source fields are injected by the host. This is a parameter
contract, not an intent router or an alternate implementation of the tools.
"""
from .agent_runtime import CAPABILITY_ENUM_ARGUMENTS
TEXT={'type':'string','minLength':1,'maxLength':2000}
QUOTES=frozenset(('request_quote','response_quote','selection_quote','confirmation_quote'))
PRIVATE=frozenset(('turn_id','source_version','feedback_id','parent_turn_id','target_child_id'))
OPTIONAL={
 'learning.explain':('support_level','material_scope','question_source','question_relation'),'learning.evaluate_response':('question_asked',),
 'task.start':('selector',),'task.resume':('selector',),
 'task.complete':('confirmed_checks','revoked_checks','completion_evidence_type'),
 'checkin.update_context':('energy_level','fixed_commitment_minutes','fixed_commitment_note','tomorrow_event_subject','tomorrow_event_title','tomorrow_event_minutes'),
 'recall.add':('knowledge_point',),'homework.add':('expected_minutes',),
 'learning_inputs.propose_revision':('changes','change_quotes'),
 'companion.activity_feedback':('execution','experience'),
 'learning_inputs.review_unplaced':('not_before','selector'),'learning_inputs.review_reclaimed_scope':('remaining_minutes','remaining_scope_quote','selector'),
 'voice.configure_wake_word':('candidate',),'mistake.capture_control':('answer','corrected_prompt'),
}
NUMBERS={'support_level':(1,6),'expected_minutes':(5,180),'minutes':(1,120),'remaining_minutes':(1,1440),
 'fixed_commitment_minutes':(0,180),'tomorrow_event_minutes':(0,60)}

def schema(spec):
 name=spec['name']
 if name=='mistake.learn':
  from .realtime_native_mistake_learning import public_schema
  return public_schema()
 if name in {'learning.review_schedule','learning.review_revisit'}:
  from .realtime_native_document_controls import public_schema
  return public_schema(name)
 choices=dict(CAPABILITY_ENUM_ARGUMENTS.get(name,{}))
 if name=='tutor.record_feedback':choices['outcome']=('understood','still_stuck','answer_attempt')
 if name=='reflection.update':choices['field']=('learned','difficult','next_action')
 if name=='mistake.review':choices['decision']=('exclude',)
 if name=='mistake.diagnose':choices['error_type']=('concept_gap','prerequisite_gap','reading_gap','method_gap','execution_error','retrieval_failure','transfer_gap','incidental','unknown')
 if name=='learning.explain':choices.update(material_scope=('standalone_concept','prepared_material'),question_source=('spoken_request','current_question'))
 if name=='companion.activity_feedback':choices.update(operation=('feedback','decline','request_resume','defer_resume'),execution=('completed','partial','not_executed','unknown','refused','stopped'),experience=('comfortable','neutral','fatigued','uncomfortable','unknown'))
 fields=(set(spec['required_arguments'])|set(choices)|set(OPTIONAL.get(name,()))) - QUOTES - PRIVATE
 if name in {'learning.evaluate_response','diagnostic.submit','exam.submit','pet_vocabulary.submit','mistake.submit_response','mistake.dialogue','tutor.capture_check_response'}:fields-={'answer','response','utterance'}
 if name=='profile.switch':fields.add('target_child_name')
 if name in {'task.start','task.resume'}:fields.add('selector')
 properties={}
 for field in sorted(fields):
  if field in choices:properties[field]={'type':'string','enum':list(choices[field])}
  elif field in NUMBERS:
   lo,hi=NUMBERS[field];properties[field]={'type':'integer','minimum':lo,'maximum':hi}
  elif field=='enabled':properties[field]={'type':'boolean'}
  elif field in {'confirmed_checks','revoked_checks'}:properties[field]={'type':'array','maxItems':20,'items':TEXT}
  elif field=='items':
   properties[field]={'type':'array','minItems':2,'maxItems':12,'items':{'type':'object','properties':{'subject':TEXT,'summary':TEXT,'title':TEXT,'expected_minutes':{'type':'integer','minimum':5,'maximum':180}},'required':['subject','summary' if name=='recall.add_batch' else 'title'],'additionalProperties':False}}
  elif field=='selector':
   from .task_start_semantics import SUBJECTS,KINDS,SOURCES
   properties[field]={'type':'object','properties':{'subject':{'type':['string','null'],'enum':[None,*SUBJECTS,*SUBJECTS.values()]},'task_kind':{'type':['string','null'],'enum':[None,*sorted(KINDS)]},'source_type':{'type':['string','null'],'enum':[None,*sorted(SOURCES)]},'relative_order':{'type':['string','null'],'enum':[None,'displayed_question']},'title_query':{'type':['string','null'],'maxLength':120}},'additionalProperties':False}
  elif field in {'changes','change_quotes'}:properties[field]={'type':'object','maxProperties':12,'additionalProperties':TEXT}
  else:properties[field]=dict(TEXT)
  if name=='learning.explain' and field=='question_relation':
   properties[field]['description']='仅明确追问上一轮同一问题或例题时选 followup；新问题或新主题选 new；不确定时先澄清。不得自行指定旧讲解编号。'
 required=[k for k in spec['required_arguments'] if k in properties and not (name=='learning.explain' and k=='question')]
 if name=='profile.switch':required=['target_child_name']
 return {'type':'object','properties':properties,'required':required,'additionalProperties':False}


def compose_arguments(c,session,voice,capability,arguments):
 """Resolve host data after semantic selection, without reclassifying speech."""
 from .realtime_capability_tools import _fail
 args=dict(arguments)
 if capability.name=='learning.explain':
  args['question_source']=args.get('question_source','spoken_request')
  if args['question_source']=='spoken_request':args['question']=voice['transcript']
 for field in QUOTES:
  if field in capability.required_arguments or field in args:args[field]=voice['transcript']
 if capability.name=='profile.switch':
  target=args.pop('target_child_name',None)
  rows=c.execute("SELECT child_id FROM child_profile WHERE status='active' AND nickname=?",(target,)).fetchall()
  if len(rows)!=1 or rows[0][0]==session['child_id']:_fail('REALTIME_PROFILE_TARGET_NOT_UNIQUE')
  args['target_child_id']=rows[0][0]
 if capability.name in {'learning.evaluate_response','diagnostic.submit','exam.submit','pet_vocabulary.submit','mistake.submit_response'}:
  args['response' if capability.name=='learning.evaluate_response' else 'answer']=voice['transcript']
 if capability.name=='mistake.dialogue':args['utterance']=voice['transcript']
 if capability.name=='task.reschedule':args['reason']=voice['transcript']
 return args


def validate_public_arguments(capability,args):
 from .realtime_capability_tools import PREPARE,META,_fail
 if capability.name==PREPARE:
  if set(args)-set(META):_fail('REALTIME_CAPABILITY_ARGUMENTS_INVALID')
  return
 spec={'name':capability.name,'required_arguments':capability.required_arguments}
 contract=schema(spec)
 if set(args)-set(contract['properties']):_fail('REALTIME_CAPABILITY_ARGUMENTS_INVALID')
 for key in contract.get('required',[]):
  if key not in args:_fail('REALTIME_CAPABILITY_ARGUMENTS_INVALID')
 if not conforms(contract,args):_fail('REALTIME_CAPABILITY_ARGUMENTS_INVALID')


def conforms(contract,value,depth=0):
 if depth>12:return False
 if 'oneOf' in contract:return sum(conforms(item,value,depth+1) for item in contract['oneOf'])==1
 if 'anyOf' in contract:return any(conforms(item,value,depth+1) for item in contract['anyOf'])
 if 'enum' in contract and value not in contract['enum']:return False
 kinds=contract.get('type',[]);kinds=[kinds] if isinstance(kinds,str) else kinds
 if not kinds:return True
 for kind in kinds:
  if kind=='null' and value is None:return True
  if kind=='string' and type(value) is str:
   return contract.get('minLength',0)<=len(value)<=contract.get('maxLength',16000)
  if kind in {'integer','number'} and type(value) in ((int,) if kind=='integer' else (int,float)):
   return contract.get('minimum',-1e12)<=value<=contract.get('maximum',1e12)
  if kind=='boolean' and type(value) is bool:return True
  if kind=='array' and type(value) is list:
   return contract.get('minItems',0)<=len(value)<=contract.get('maxItems',128) and all(conforms(contract.get('items',{}),item,depth+1) for item in value)
  if kind=='object' and type(value) is dict:
   properties=contract.get('properties',{});additional=contract.get('additionalProperties',True)
   if len(value)>contract.get('maxProperties',64) or any(key not in value for key in contract.get('required',[])):return False
   return all(conforms(properties[key],item,depth+1) if key in properties else
     False if additional is False else conforms(additional,item,depth+1) if type(additional) is dict else True for key,item in value.items())
 return False
