"""Finite runtime facts; never raw response text, result objects or private IDs."""
import json

def extend(c,row,step,facts):
 if facts.get('execution_status')!='completed':return
 cap=step['capability_name'];saved=json.loads(step['result_json']) if step['result_json'] else {}
 result=saved.get('result') or {};receipt=saved.get('execution_receipt') or saved.get('client_bridge_receipt') or {}
 output=receipt.get('output') or {}
 from .agent_runtime import AgentRuntimeStore
 from .realtime_native_client import audit,complete_key
 delivered=audit(c,complete_key(row['agent_turn_id'])) or {}
 if delivered.get('local_delivery') is not None:facts['local_delivery']=delivered['local_delivery']
 facts['effect_confirmed']=not AgentRuntimeStore.execution_result_blocked(saved)
 if not facts['effect_confirmed']:
  facts['business_status']='blocked'
  return
 if saved.get('requires_child_reply') is True:facts['requires_child_reply']=True
 if cap in {'device.status','device.connect'}:
  devices=output.get('devices') or []
  facts['devices']=[{k:item[k] for k in ('device_type','track_live','frame_confirmed','monitor_active','lease_confirmed') if k in item and (type(item[k]) is bool or k=='device_type' and item[k] in {'camera','document_camera'})} for item in devices if type(item) is dict]
  # A frame proves only capture readiness, never a person or visual content.
  facts['visual_content_inspected']=False
 if cap in {'workspace.navigate','mistake.open'}:
  from .realtime_native_client import audit,complete_key
  delivered=audit(c,complete_key(row['agent_turn_id'])) or {}
  ui=delivered.get('output') or output
  if ui.get('workspace_opened') is True and ui.get('workspace') in {'learning','mistake_book'} and ui.get('content_status') in {'ready','empty'}:
   facts.update(workspace_opened=True,workspace=ui['workspace'],content_status=ui['content_status'])
 session_state=result.get('state') or (result.get('session') or {}).get('state')
 if session_state in {'check_in','classroom_recall','homework_capture','plan_review','study','break','reflection','evolving','completed'}:facts['session_state']=session_state
 if cap=='session.transition' and result.get('state') in {'check_in','classroom_recall','homework_capture','plan_review','study','break','reflection','evolving','completed'}:facts['session_state']=result['state']
 if cap=='profile.switch':facts['profile_switched']=True
 if cap=='privacy.pause_all':facts['listening_stopped']=True
 if cap in {'learning.capture_question','document.capture_question','document.retry_capture','document.confirm_ocr','document.correct_ocr','document.cancel_capture'}:
  facts['document_stage_confirmed']=cap.rsplit('.',1)[-1]
  facts['material_content_verified']=False
 if cap in {'controlled_content.play','controlled_game.play','controlled_content.stop','controlled_game.stop','speech.stop','tutor.playback_control','pet.listening_control'}:
  facts['playback_operation_confirmed']=cap
 if cap in {'planning.generate','planning.reorder','planning.describe','recall.add','recall.add_batch','recall.none','homework.add','homework.add_batch','homework.none','checkin.update_context'}:
  facts['saved_operation']=cap
  for key in ('total_minutes','remaining_minutes','task_count','item_count'):
   value=result.get(key)
   if type(value) is int and 0<=value<=100000:facts[key]=value
 if cap=='mistake.learn':
  from .realtime_native_mistake_learning import completion_facts
  facts.update(completion_facts(c,row,output))
 if cap.startswith(('learning.','tutor.','diagnostic.','exam.','mistake.','writing.','oral.','native_writing.','pet_vocabulary.')):
  facts['local_learning_result_available']=bool(saved)
  facts['native_dialogue_is_score']=False
  # Formal grades/answers/source content are deliberately not copied. Their
  # existing dedicated local receipt/teaching presentation is authoritative.
