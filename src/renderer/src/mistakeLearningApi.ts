import {ApiError,request} from './api'
import type {MistakeLearningLibrary,MistakeLearningMutation,MistakeLearningOperation,MistakeLearningState,MistakeLearningIntake,MistakeLearningIntakeReceipt} from './mistakeLearningTypes'
import type {MistakeWorkspaceVoiceCommand} from './mistakeWorkspaceVoice'
const opaque=(id:string):string=>{if(!/^[A-Za-z0-9_-]{1,160}$/.test(id))throw new ApiError('学习记录编号尚未核对，请刷新。');return encodeURIComponent(id)}
export function checkedMistakeLearningState(value:MistakeLearningState,childId:string,mistakeId?:string):MistakeLearningState{
 if(!value||value.version!==1||value.child_id!==childId||mistakeId&&value.mistake_id!==mistakeId||!value.source||typeof value.source.question!=='string'||typeof value.source.original_answer!=='string'||!Number.isSafeInteger(value.revision)||typeof value.source_revision!=='string'||!Array.isArray(value.practice_items)||!Array.isArray(value.turns)||!value.today)throw new ApiError('这次读取的学习记录与当前孩子或题目不一致，请刷新。',undefined,'MISTAKE_LEARNING_SCOPE_CHANGED')
 if(value.focus&&(value.focus.child_id!==childId||value.focus.mistake_id!==value.mistake_id||value.focus.source_revision!==value.source_revision))throw new ApiError('当前题目已变化，请刷新后继续。',undefined,'MISTAKE_LEARNING_SCOPE_CHANGED')
 return value
}
export const mistakeLearningApi={
 intake:async(childId:string,body:MistakeLearningIntake):Promise<MistakeLearningIntakeReceipt>=>{const result=await request<MistakeLearningIntakeReceipt>(`/v1/children/${opaque(childId)}/mistake-learning/cards`,{method:'POST',body:JSON.stringify(body)},false,true);if(result?.version!==1||result.child_id!==childId||result.saved!==true||typeof result.mistake_id!=='string')throw new ApiError('这道题的保存结果还没有核对成功。');return result},
 release:async(childId:string,sessionId:string,expectedGeneration:number):Promise<void>=>{if(!Number.isSafeInteger(expectedGeneration)||expectedGeneration<1)throw Error('MISTAKE_LEARNING_SCOPE_CHANGED');await request(`/v1/children/${opaque(childId)}/mistake-learning/focus/release`,{method:'POST',body:JSON.stringify({session_id:sessionId,expected_generation:expectedGeneration})},false,true)},
 action:async(mistakeId:string,childId:string,actionId:string):Promise<MistakeLearningState>=>{if(!/^[A-Za-z0-9_:-]{1,200}$/.test(actionId))throw Error('MISTAKE_LEARNING_LOCAL_INVALID');return checkedMistakeLearningState(await request<MistakeLearningState>(`/v1/mistakes/${opaque(mistakeId)}/learning/actions/${encodeURIComponent(actionId)}?child_id=${opaque(childId)}`,{cache:'no-store'}),childId,mistakeId)},
 library:async(childId:string,signal?:AbortSignal):Promise<MistakeLearningLibrary>=>{
  const result=await request<MistakeLearningLibrary>(`/v1/children/${opaque(childId)}/mistake-learning`,{signal,cache:'no-store'})
  if(result?.version!==1||result.child_id!==childId||!Array.isArray(result.items)||!result.today)throw new ApiError('错题列表暂时没有读到，请刷新。',undefined,'MISTAKE_LEARNING_SCOPE_CHANGED')
  for(const item of result.items)checkedMistakeLearningState(item,childId)
  return result
 },
 state:async(mistakeId:string,childId:string,sessionId?:string,signal?:AbortSignal):Promise<MistakeLearningState>=>checkedMistakeLearningState(await request<MistakeLearningState>(`/v1/mistakes/${opaque(mistakeId)}/learning?child_id=${opaque(childId)}${sessionId?`&session_id=${opaque(sessionId)}`:''}`,{signal,cache:'no-store'}),childId,mistakeId),
 mutate:async(mistakeId:string,operation:MistakeLearningOperation,body:MistakeLearningMutation,signal?:AbortSignal):Promise<MistakeLearningState>=>{
  if(!['analyze','practice','coach','answer','focus','review'].includes(operation))throw new ApiError('这项学习操作暂不支持。')
  return checkedMistakeLearningState(await request<MistakeLearningState>(`/v1/mistakes/${opaque(mistakeId)}/learning/${operation}`,{method:'POST',body:JSON.stringify(body),signal},false,true),body.child_id,mistakeId)
 },
 captureReview: (sessionId:string, childId:string, leaseId:string, imageBase64:string, signal:AbortSignal):Promise<{artifact_id:string}>=>
  request(`/v1/sessions/${opaque(sessionId)}/mistake-review/ocr`,{method:'POST',signal,body:JSON.stringify({child_id:childId,lease_id:leaseId,image_base64:imageBase64,client_action_id:crypto.randomUUID()})},false,true),
 claimWorkspaceVoice: async (sessionId:string, childId:string, voiceInputId:string, kind:MistakeWorkspaceVoiceCommand|'answer', answer?:{after_ms:number;mistake_id:string;source_revision:string}):Promise<'accepted'|'safety'|'stale'|'already_handled'>=>{
  const result=await request<{claimed?:boolean;already_handled?:true;safety?:true;stale?:true}>(`/v1/sessions/${opaque(sessionId)}/mistake-workspace/voice-claim`,{method:'POST',signal:AbortSignal.timeout(8000),body:JSON.stringify({child_id:childId,voice_input_id:voiceInputId,kind,...answer})},false,true)
  if(result?.claimed===true)return 'accepted'
  if(result?.claimed===false&&result.already_handled===true)return 'already_handled'
  if(result?.claimed===false&&result.safety===true)return 'safety'
  if(result?.claimed===false&&result.stale===true)return 'stale'
  throw new ApiError('这次语音归属尚未核对，请重新说一遍。')
 }
}
type MistakeAnswerDraft={practice_item_id:string;text:string}
export type MistakeLearningDraft={version:1;child_id:string;mistake_id:string;source_revision:string;coach:string;answer:MistakeAnswerDraft|null;coach_variant_answer?:MistakeAnswerDraft|null}
export type MistakeLearningPending={version:1;child_id:string;mistake_id:string;source_revision:string;operation:Exclude<MistakeLearningOperation,'focus'>;body:MistakeLearningMutation;status:'pending'|'unknown'|'failed';waiting_for_action_id?:string;not_found?:boolean}
const key=(kind:'draft'|'pending',childId:string,mistakeId:string)=>`hsc-cloud-mistake-${kind}-v1:${childId}:${mistakeId}`
function validLocal(value:unknown,childId:string,mistakeId:string,kind:'draft'|'pending'):value is MistakeLearningDraft|MistakeLearningPending{
 if(!value||typeof value!=='object')return false
 const v=value as Record<string,any>
 if(v.version!==1||v.child_id!==childId||v.mistake_id!==mistakeId||typeof v.source_revision!=='string')return false
 if(kind==='draft'){
  const validAnswer=(answer:unknown):boolean=>{
   if(answer===null)return true
   const item=answer as Partial<MistakeAnswerDraft>|undefined
   return !!item&&typeof item.practice_item_id==='string'&&typeof item.text==='string'&&item.text.length<=4000
  }
  return typeof v.coach==='string'&&v.coach.length<=4000&&validAnswer(v.answer)
   &&(v.coach_variant_answer===undefined||validAnswer(v.coach_variant_answer))
 }
 return ['pending','unknown','failed'].includes(v.status)&&['analyze','practice','coach','answer'].includes(v.operation)&&v.body&&v.body.child_id===childId&&typeof v.body.client_action_id==='string'&&/^[A-Za-z0-9_:-]{1,200}$/.test(v.body.client_action_id)&&(!v.body.utterance||typeof v.body.utterance==='string'&&v.body.utterance.length<=4000)
}
export function readMistakeLearningLocal<T extends MistakeLearningDraft|MistakeLearningPending>(kind:'draft'|'pending',childId:string,mistakeId:string):T|null{
 if(kind==='pending')return (pendingMistakeLearningRequests(childId,mistakeId)[0]??null) as T|null
 const text=localStorage.getItem(key(kind,childId,mistakeId));if(text===null)return null
 if(text.length>24000)throw Error('MISTAKE_LEARNING_LOCAL_INVALID')
 const value=JSON.parse(text);if(!validLocal(value,childId,mistakeId,kind))throw Error('MISTAKE_LEARNING_LOCAL_INVALID');return value as T
}
export function archiveMistakeLearningDraft(value:MistakeLearningDraft):void{
 const k=key('draft',value.child_id,value.mistake_id)+':'+value.source_revision,raw=JSON.stringify(value)
 if(Object.keys(localStorage).filter(item=>item.startsWith(key('draft',value.child_id,value.mistake_id)+':')).length>=32&&localStorage.getItem(k)===null)throw Error('MISTAKE_LEARNING_LOCAL_FULL')
 localStorage.setItem(k,raw);if(localStorage.getItem(k)!==raw)throw Error('MISTAKE_LEARNING_LOCAL_UNCONFIRMED')
}
export function saveMistakeLearningLocal(kind:'draft'|'pending',value:MistakeLearningDraft|MistakeLearningPending):void{
 const serialized=kind==='pending'?{...value,saved_at:Date.now()}:value;const text=JSON.stringify(serialized);if(text.length>24000)throw Error('MISTAKE_LEARNING_LOCAL_FULL')
 const base=key(kind,value.child_id,value.mistake_id),k=kind==='pending'?base+':'+(value as MistakeLearningPending).body.client_action_id:base;if(kind==='pending'&&localStorage.getItem(k)===null&&Object.keys(localStorage).filter(k=>k.startsWith(base+':')).length>=64)throw Error('MISTAKE_LEARNING_LOCAL_FULL');localStorage.setItem(k,text);if(localStorage.getItem(k)!==text)throw Error('MISTAKE_LEARNING_LOCAL_UNCONFIRMED')
}
export function clearMistakeLearningPending(childId:string,mistakeId:string,actionId:string):void{localStorage.removeItem(key('pending',childId,mistakeId)+':'+actionId)}

export type MistakeLearningIntakeLocal={version:1;child_id:string;question:string;submitted_answer:string;client_action_id:string;analysis_action_id:string;status:'draft'|'unknown'|'saved';session_id?:string;mistake_id?:string}
const intakeKey=(childId:string)=>'hsc-cloud-mistake-intake-v1:'+childId
export function readMistakeLearningIntake(childId:string):MistakeLearningIntakeLocal|null{const raw=localStorage.getItem(intakeKey(childId));if(raw===null)return null;if(raw.length>18000)throw Error('MISTAKE_LEARNING_LOCAL_INVALID');const value=JSON.parse(raw);if(value?.version!==1||value.child_id!==childId||typeof value.question!=='string'||typeof value.submitted_answer!=='string'||typeof value.client_action_id!=='string'||typeof value.analysis_action_id!=='string'||!['draft','unknown','saved'].includes(value.status)||value.question.length>12000||value.submitted_answer.length>8000||value.session_id!==undefined&&typeof value.session_id!=='string')throw Error('MISTAKE_LEARNING_LOCAL_INVALID');return value}
export function saveMistakeLearningIntake(value:MistakeLearningIntakeLocal):void{const raw=JSON.stringify(value);if(raw.length>18000)throw Error('MISTAKE_LEARNING_LOCAL_FULL');localStorage.setItem(intakeKey(value.child_id),raw);if(localStorage.getItem(intakeKey(value.child_id))!==raw)throw Error('MISTAKE_LEARNING_LOCAL_UNCONFIRMED')}
export function clearMistakeLearningIntake(childId:string):void{localStorage.removeItem(intakeKey(childId))}

export function pendingMistakeLearningRequests(childId:string,mistakeId:string):MistakeLearningPending[]{return Object.keys(localStorage).filter(k=>k.startsWith(key('pending',childId,mistakeId)+':')).map(k=>{const raw=localStorage.getItem(k)!;if(raw.length>24000)throw Error('MISTAKE_LEARNING_LOCAL_INVALID');const value=JSON.parse(raw);if(!validLocal(value,childId,mistakeId,'pending'))throw Error('MISTAKE_LEARNING_LOCAL_INVALID');return value as MistakeLearningPending}).sort((a,b)=>((b as MistakeLearningPending&{saved_at?:number}).saved_at??0)-((a as MistakeLearningPending&{saved_at?:number}).saved_at??0))}
