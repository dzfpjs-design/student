"""Concrete native tool -> existing immutable local source/target composition."""
import json
KEY='native_source_targets_v1'
CONTROLS=frozenset(('task.start','task.resume','task.complete','task.reschedule','exam.pause','exam.resume','exam.cancel','diagnostic.replay','diagnostic.cancel','tutor.playback_control','companion.end_activity'))


def compose(c,session,voice,capture,capability,args,runtime,*,arrival_id,turn_id,current_graph_version=None):
 from .realtime_capability_tools import _fail,_hash
 if capability=='mistake.learn':
  from .realtime_native_mistake_learning import compose as compose_mistake
  return compose_mistake(c,session,voice,capture,args,turn_id=turn_id,current_graph_version=current_graph_version)
 sid= session['session_id'];child=session['child_id'];action={};manifest={};targets={};args=dict(args)
 if capability in {'task.start','task.complete'}:
  from .writing_daily_followup import capture_arrival
  capture_arrival(c,arrival_id)
 if capability=='learning.explain' and args.get('question_source')=='current_question':
  if args.get('question_relation')=='followup':_fail('TUTOR_FOLLOWUP_SOURCE_UNAVAILABLE')
  reference=runtime.captured_question(capture)
  if not reference or capture.get('question_validation')!='valid':_fail('REALTIME_QUESTION_SOURCE_REQUIRED')
  if reference['kind']=='learning_reply':
   from .ordinary_learning_text_projection import _source,TextProjectionBlocked
   try:source=_source(c,reference)
   except (TextProjectionBlocked,ValueError,KeyError,TypeError):_fail('REALTIME_QUESTION_SOURCE_REQUIRED')
   if not source.get('question'):_fail('REALTIME_QUESTION_SOURCE_REQUIRED')
   args.update(question=source['question'],subject=source['subject'])
   manifest['native_authorized_spoken_question_v1']={'kind':'trusted_tutor_question','reference_sha256':_hash(reference),'question_sha256':_hash(source['question'])}
  elif reference['kind']=='diagnostic':
   from .learning_question_help import snapshot
   source=snapshot(c,sid,child)
   if not source or reference['binding']!={k:source[k] for k in ('prompt_id','item_version')}:_fail('REALTIME_QUESTION_SOURCE_REQUIRED')
   args.update(question=source['question'],subject=source['subject'])
   action.update(diagnostic_prompt_id=source['prompt_id'],diagnostic_item_version=source['item_version'])
  else:_fail('REALTIME_QUESTION_SOURCE_REQUIRED')
 if capability=='learning.explain':
  if not 2<=len(args.get('question',''))<=2000:_fail('REALTIME_QUESTION_TOO_LONG')
  if args.get('question_relation')=='followup':
   from .tutor_followup_binding import latest_binding
   binding=latest_binding(c,session_id=sid,child_id=child)
   if not binding:_fail('TUTOR_FOLLOWUP_SOURCE_UNAVAILABLE')
   manifest['tutor_followup_binding']=binding
   action.update(tutor_followup_binding=binding,tutor_request_action_id=turn_id+'-followup')
  else:action['tutor_request_action_id']='native-tutor-'+turn_id
 elif capability in {'tutor.record_feedback','tutor.more_hint'}:
  from datetime import datetime,timezone
  prior=c.execute('SELECT * FROM tutor_turn WHERE session_id=? AND child_id=? ORDER BY created_at DESC,turn_id DESC LIMIT 1',(sid,child)).fetchone()
  if not prior or (datetime.now(timezone.utc)-datetime.fromisoformat(prior['created_at'].replace('Z','+00:00'))).total_seconds()>1800:_fail('REALTIME_TUTOR_SOURCE_REQUIRED')
  if capability=='tutor.more_hint' and prior['hint_level']>=6:_fail('REALTIME_TUTOR_SOURCE_REQUIRED')
  manifest['native_tutor_source_v1']={k:prior[k] for k in ('turn_id','subject','question','hint_level')}
  action['tutor_turn_id']=prior['turn_id']
  if capability=='tutor.record_feedback':
   if len(voice['transcript'])>2000:_fail('REALTIME_FEEDBACK_TOO_LONG')
   action['feedback_text']=voice['transcript']
 if capability in {'diagnostic.submit','exam.submit','pet_vocabulary.submit','mistake.submit_response','mistake.dialogue'}:
  reference=runtime.captured_question(capture)
  if not reference or capture.get('question_validation')!='valid':_fail('REALTIME_QUESTION_SOURCE_REQUIRED')
  target=reference['binding']
  if capability=='diagnostic.submit':action.update(diagnostic_prompt_id=target['prompt_id'],diagnostic_item_version=target['item_version'])
  elif capability=='exam.submit':action.update(exam_run_id=target['run_id'],exam_input_binding=target)
  elif capability=='pet_vocabulary.submit':action.update(vocabulary_prompt_id=target['prompt_session_id'],vocabulary_card_version=target['card_version'])
  else:action['mistake_answer_binding']=target
 if capability in {'task.start','task.resume'}:
  from .task_start_binding import TaskStartBinding as T
  selector=args.pop('selector',None)
  if capability=='task.start':
   target=T.start_snapshot(c,sid,child)
   if selector:
    from .task_start_semantics import SUBJECTS
    subject=selector.get('subject');subject=SUBJECTS.get(subject,subject)
    target=T.select_start_target(target,{'subject':subject,'source_type':selector.get('source_type'),'task_type':selector.get('task_kind'),'title_query':selector.get('title_query'),'conflict':False})
   if target.get('status')!='ready' or not target.get('item'):_fail('REALTIME_TASK_TARGET_REQUIRED')
   entries=json.loads(c.execute('SELECT snapshot_json FROM learning_input_arrival WHERE arrival_id=?',(arrival_id,)).fetchone()[0])
   entries.append({'kind':'task_navigation_arrival','snapshot':target,'local_only':True,'cloud_transmission_allowed':False})
   c.execute('UPDATE learning_input_arrival SET snapshot_json=? WHERE arrival_id=?',(json.dumps(entries,ensure_ascii=False),arrival_id))
   manifest['task_start_arrival']=target
   manifest['task_start_binding']={**{k:target['item'][k] for k in ('plan_item_id','source_type','source_ref')},'operation':'start'}
   targets['task_start']=target
  else:
   target=T.resume_snapshot(c,sid,child,runtime.captured_question(capture))
   if selector:
    selected={k:selector.get(k) for k in ('subject','task_kind','relative_order')}
    from .task_start_semantics import SUBJECTS
    if selected['subject'] in SUBJECTS.values():selected['subject']=next(key for key,value in SUBJECTS.items() if value==selected['subject'])
    target=T.select_resume_target(target,selected)
   if target.get('status')!='ready' or not target.get('item'):_fail('REALTIME_TASK_TARGET_REQUIRED')
   targets['task_resume']=target
   item=target['item']
   if item.get('goal_prompt_binding'):manifest['goal_prompt_binding']=item['goal_prompt_binding']
   if item.get('operation')=='start':manifest['task_start_binding']={k:item[k] for k in ('plan_item_id','source_type','source_ref','operation')}
 elif capability=='task.reschedule':
  from .task_source_recovery import deferral_target
  targets['task_deferral']=deferral_target(c,child,sid)
 elif capability=='task.complete':
  from .realtime_native_authority import _task
  targets['task']=_task(c,sid,child)
 elif capability in {'exam.pause','exam.resume','exam.cancel'}:
  rows=c.execute("SELECT run_id FROM specialized_exam_run WHERE child_id=? AND session_id=? AND status IN ('active','paused')",(child,sid)).fetchall()
  if len(rows)!=1:_fail('REALTIME_EXAM_TARGET_REQUIRED')
  targets['exam_run_id']=rows[0][0];action['exam_run_id']=rows[0][0]
 elif capability.startswith('diagnostic.') and capability!='diagnostic.start':
  reference=runtime.captured_question(capture)
  if not reference or reference['kind']!='diagnostic' or capture.get('question_validation')!='valid':_fail('REALTIME_QUESTION_SOURCE_REQUIRED')
  target={k:reference['binding'][k] for k in ('prompt_id','item_version')}
  targets['diagnostic']=target;action.update(diagnostic_prompt_id=target['prompt_id'],diagnostic_item_version=target['item_version'])
 elif capability=='tutor.playback_control':
  row=c.execute('SELECT playback_id,source_version,generation,revision FROM tutor_playback_checkpoint WHERE child_id=? AND session_id=? ORDER BY rowid DESC LIMIT 1',(child,sid)).fetchone()
  if not row:_fail('REALTIME_TUTOR_PLAYBACK_REQUIRED')
  targets['tutor']=dict(row);action.update(tutor_playback_id=row['playback_id'],tutor_source_version=row['source_version'])
 if capability=='companion.activity_feedback':
  from .companion_activity_outcome import CompanionActivityOutcomeService
  service=CompanionActivityOutcomeService(runtime.database)
  candidates=[];active=[]
  for row in c.execute('SELECT outcome_id FROM companion_activity_outcome WHERE child_id=? AND session_id=?',(child,sid)).fetchall():
   activity=service._detail(c,child,row[0])['activity']
   if activity['source_valid'] and activity['resume']['status'] not in {'confirmed','deferred'}:
    binding={'outcome_id':row[0],'expected_revision':activity['revision'],'child_id':child,'session_id':sid}
    candidates.append(binding)
    if activity['lifecycle']['status']=='active':active.append(binding)
  binding=active[0] if len(active)==1 else candidates[0] if len(candidates)==1 else None
  if binding is None:_fail('REALTIME_ACTIVITY_TARGET_REQUIRED')
  manifest['companion_activity_outcome_binding']=binding
 if capability in {'learning_inputs.review_unplaced','learning_inputs.review_reclaimed_scope'}:
  snapshot=runtime.capture_unplaced_arrival(c,arrival_id)
  selector=args.pop('selector',None);candidates=snapshot['candidates'];focus=(snapshot.get('dialogue') or {}).get('focus')
  if selector:
   candidates=[item for item in candidates if (not selector.get('subject') or item.get('subject')==selector['subject'])
     and (not selector.get('title_query') or selector['title_query'] in item.get('title',''))]
  chosen=(next((item for item in candidates if focus and item['binding']==focus.get('binding')),None)
    or (candidates[0] if len(candidates)==1 else None))
  if not chosen or not chosen.get('source_current'):_fail('REALTIME_CARRYOVER_TARGET_REQUIRED')
  if capability=='learning_inputs.review_unplaced' and args.get('decision') not in chosen['allowed_actions']:_fail('REALTIME_CARRYOVER_DECISION_NOT_AVAILABLE')
  manifest['learning_input_unplaced_binding']=chosen['binding'];targets['unplaced']=snapshot
  if capability=='learning_inputs.review_reclaimed_scope':
   if args.get('operation')=='confirm_scope':
    from .learning_input_revision import duration_minutes
    import re
    scope=args.get('remaining_scope_quote')
    estimates=[duration_minutes(value) for value in re.findall(r'([零〇一二两三四五六七八九十百0-9]+)分钟',voice['transcript'])]
    literal=type(scope) is str and bool(scope.strip()) and scope in voice['transcript'] and estimates==[args.get('remaining_minutes')]
    if not literal:
     dialogue=snapshot.get('dialogue') or {};draft=dialogue.get('scope_draft') or {}
     if (draft.get('binding')!=chosen['binding'] or not dialogue.get('offer_turn_id')
         or (draft.get('scope') or {}).get('quote')!=scope
         or (draft.get('estimate') or {}).get('minutes')!=args.get('remaining_minutes')):_fail('REALTIME_CARRYOVER_SCOPE_CONFIRMATION_REQUIRED')
     manifest['learning_input_scope_offer']={'draft':draft,'offer_turn_id':dialogue['offer_turn_id']}
 if capability=='tutor.capture_check_response':
  from .tutor_check_response import capture_check_binding
  target=capture_check_binding(c,sid,child,capture)
  if target is None:_fail('REALTIME_TUTOR_CHECK_SOURCE_REQUIRED')
  args={**{k:target[k] for k in ('turn_id','source_version')},'response':voice['transcript']}
  targets['tutor_check']=target
 elif capability=='tutor.retry_check_coaching':
  from .check_coaching_retry_control import snapshot,target as retry_target
  target=snapshot(c,sid,child)
  if target is None:_fail('REALTIME_TUTOR_RETRY_SOURCE_REQUIRED')
  resolved=retry_target(c,sid,child,target)
  args={k:resolved[k] for k in ('feedback_id','parent_turn_id')}
  targets['check_coaching_retry']=target
 if targets:manifest[KEY]={'version':1,'capability':capability,'targets':targets,'arguments_sha256':_hash(args)}
 return {'arguments':args,'action':action,'manifest':manifest}


def inspect_control(c,turn_id,*,allow_planned=False):
 from .realtime_capability_tools import _hash,_immutable_capture
 from .agent_runtime import AgentRuntimeStore,InvalidAgentPlanError
 from .realtime_native_authority import _execution_authorizer
 from .privacy_operation_receipt import generation
 def fail():raise InvalidAgentPlanError('NATIVE_RUNTIME_TARGET_CHANGED')
 row=c.execute('''SELECT t.*,s.capability_name,s.arguments_json,s.status AS step_status,s.idempotency_key,p.status AS plan_status,p.context_manifest_json
   FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id) WHERE t.agent_turn_id=? AND s.step_index=0''',(turn_id,)).fetchone()
 if not row:return None
 manifest=json.loads(row['context_manifest_json']);source=manifest.get(KEY)
 if source is None:return None
 packet=manifest.get('qwen_realtime_capability_v1');action=json.loads(row['action_json'])
 if packet and packet.get('native_sources_sha256')!=_hash({key:manifest.get(key) for key in packet.get('native_manifest_keys',[])}):fail()
 if not packet or action.get('source')!='qwen_realtime_capability' or source['capability']!=row['capability_name'] or source['arguments_sha256']!=_hash(json.loads(row['arguments_json'])) or packet['storage_generation']!=generation(c):fail()
 voice=c.execute('SELECT * FROM voice_input WHERE voice_input_id=? AND child_id=? AND session_id=?',(packet['identity']['voice_input_id'],row['child_id'],row['session_id'])).fetchone()
 owner=c.execute('SELECT status FROM daily_session WHERE session_id=? AND child_id=?',(row['session_id'],row['child_id'])).fetchone()
 consent=c.execute('SELECT cloud_audio_allowed,cloud_model_text_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
 if not voice or not owner or owner[0]!='active' or not consent or any(type(x) is not int or x!=1 for x in consent):fail()
 capture=AgentRuntimeStore.resolve_captured_voice(c,session_id=row['session_id'],child_id=row['child_id'],voice_input_id=voice['voice_input_id'],transcript=voice['transcript'])
 if _hash(_immutable_capture(capture))!=packet['capture_hash'] or _hash(voice['transcript'])!=packet['original_transcript_sha256']:fail()
 authorize=_execution_authorizer.get()
 if authorize is None or authorize(c,row['session_id'],row['child_id'],{'version':1,**packet['identity']},dict(voice)) is not True:fail()
 if row['step_status'] not in ({'planned','running','waiting_child'} if allow_planned else {'running'}):fail()
 cap=source['capability'];targets=source['targets']
 if cap=='task.start':
  from .task_start_binding import TaskStartBinding
  TaskStartBinding.validate_start_snapshot(c,row['session_id'],row['child_id'],targets['task_start'],turn_id=turn_id)
 elif cap=='task.resume':
  from .task_start_binding import TaskStartBinding
  TaskStartBinding.validate_resume_target(c,row['session_id'],row['child_id'],targets['task_resume'])
 elif cap=='task.reschedule':
  from .task_source_recovery import deferral_target
  if deferral_target(c,row['child_id'],row['session_id'])!=targets['task_deferral']:fail()
 elif cap=='task.complete':
  from .realtime_native_authority import _task
  if _task(c,row['session_id'],row['child_id'])!=targets['task']:fail()
 elif cap.startswith('exam.'):
  actual=c.execute("SELECT run_id FROM specialized_exam_run WHERE child_id=? AND session_id=? AND status IN ('active','paused')",(row['child_id'],row['session_id'])).fetchall()
  if len(actual)!=1 or actual[0][0]!=targets['exam_run_id']:fail()
 elif cap.startswith('diagnostic.'):
  ref=AgentRuntimeStore.captured_question(capture)
  if capture.get('question_validation')!='valid' or (ref or {}).get('binding')!=targets['diagnostic']:fail()
 elif cap=='tutor.playback_control':
  actual=c.execute('SELECT playback_id,source_version,generation,revision FROM tutor_playback_checkpoint WHERE child_id=? AND session_id=? ORDER BY rowid DESC LIMIT 1',(row['child_id'],row['session_id'])).fetchone()
  if not actual or dict(actual)!=targets['tutor']:fail()
 return {'control':'learning.retry_check_coaching' if cap=='tutor.retry_check_coaching' else cap,'targets':targets,'request_key':row['client_action_id']}


def tutor_check_source(c,turn_id):
 from .agent_runtime import AgentRuntimeStore
 from .tutor_check_response import capture_check_binding,TutorCheckError
 row=c.execute('SELECT t.*,s.arguments_json,p.context_manifest_json FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id) WHERE t.agent_turn_id=? AND s.step_index=0',(turn_id,)).fetchone()
 if not row:return None
 manifest=json.loads(row['context_manifest_json']);native=manifest.get(KEY)
 if not native or native['capability']!='tutor.capture_check_response':return None
 packet=manifest.get('qwen_realtime_capability_v1');target=native['targets']['tutor_check'];args=json.loads(row['arguments_json'])
 if not packet or packet['identity']['voice_input_id']!=target['voice_input_id'] or args!={**{k:target[k] for k in ('turn_id','source_version')},'response':row['transcript']}:raise TutorCheckError('TUTOR_CHECK_SOURCE_CHANGED')
 capture=AgentRuntimeStore.resolve_captured_voice(c,session_id=row['session_id'],child_id=row['child_id'],voice_input_id=target['voice_input_id'],transcript=row['transcript'])
 existing=c.execute("SELECT 1 FROM tutor_feedback WHERE client_action_id=? AND turn_id=? AND evidence_basis='tutor_check_response'",('tutor-check:'+turn_id,target['turn_id'])).fetchone()
 if not existing and capture_check_binding(c,row['session_id'],row['child_id'],capture)!=target:raise TutorCheckError('TUTOR_CHECK_SOURCE_CHANGED')
 return row,args


def resolve_unplaced_native(c,turn,manifest,args,snapshot):
 from .realtime_capability_tools import _hash
 from .agent_runtime import InvalidAgentPlanError
 packet=manifest.get('qwen_realtime_capability_v1');native=manifest.get(KEY)
 if packet is None or native is None:return None
 binding=manifest.get('learning_input_unplaced_binding')
 target=next((item for item in snapshot.get('candidates',[]) if item.get('binding')==binding),None)
 cap=packet['capability']
 if (cap not in {'learning_inputs.review_unplaced','learning_inputs.review_reclaimed_scope'}
     or native['targets'].get('unplaced')!=snapshot or not target or not target.get('source_current')
     or packet['arguments_sha256']!=_hash(args) or args.get('request_quote')!=turn['transcript']
     or cap=='learning_inputs.review_unplaced' and args.get('decision') not in target['allowed_actions']):
  raise InvalidAgentPlanError('NATIVE_CARRYOVER_SOURCE_CHANGED')
 return {'capability':cap,'arguments':args,'binding':binding,'scope_offer':manifest.get('learning_input_scope_offer')}
