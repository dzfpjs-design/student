import type { NativeWritingInput, NativeWritingOriginal, NativeWritingScope } from '../../shared/nativeWriting'
import type { DiagnosticItem, DiagnosticPromptResult } from './types'
import { localApi } from './api'

export type WritingPrompt = NonNullable<DiagnosticPromptResult['prompt']>
export const writingScope = (prompt: WritingPrompt): NativeWritingScope => ({
  child_id: prompt.child_id, session_id: prompt.origin_session_id, prompt_id: prompt.prompt_id,
  item_version: prompt.item_version, diagnostic_item_id: prompt.diagnostic_item_id,
})
export const sameWritingScope = (left: NativeWritingScope, right: NativeWritingScope): boolean =>
  (['child_id','session_id','prompt_id','item_version','diagnostic_item_id'] as const).every(k => left[k] === right[k])

export type TypedWritingDraft = { input_kind: 'typed'; scope: NativeWritingScope; text: string }
export const isWrittenDiagnostic=(item:DiagnosticItem|null|undefined):boolean=>
  Boolean(item && (item.answer_type==='writing' || (item.answer_type==='open_response'
    && item.assessment_mode==='open_response_rubric' && item.response_mode==='written')))
export const isSavedNativeWritingPrompt=(result:DiagnosticPromptResult,draft:TypedWritingDraft|null):boolean=>
  Boolean(result.status==='answer_saved' && result.prompt && isWrittenDiagnostic(result.prompt.item) && draft
    && sameWritingScope(writingScope(result.prompt),draft.scope))
export function restoreTypedWritingDraft(value: unknown, childId: string, sessionId: string): TypedWritingDraft | null {
  if (!value || typeof value !== 'object') return null
  const draft = value as Partial<TypedWritingDraft>
  const scope = draft.scope
  if (draft.input_kind !== 'typed' || typeof draft.text !== 'string' || !scope
    || scope.child_id !== childId || scope.session_id !== sessionId
    || !['child_id','session_id','prompt_id','item_version','diagnostic_item_id'].every(key =>
      typeof scope[key as keyof NativeWritingScope] === 'string' && scope[key as keyof NativeWritingScope].length > 0)) return null
  return { input_kind: 'typed', text: draft.text, scope: { ...scope } }
}

export type NativeWritingReceiptQuery = NativeWritingScope & { text_sha256:string; capture_action_id:string; answer_action_id:string; resume_action_id?:string }
export type NativeWritingReceipt = NativeWritingReceiptQuery & {
  state:'capture_unknown'|'captured'|'answer_saved'|'answered'|'capture_rejected'|'answer_rejected'; original:NativeWritingOriginal|null;
  result:DiagnosticPromptResult|null; local_only:true; cloud_transmission_allowed:false;
  resume?:{client_action_id:string;authorized_turn_id:string;original_authorized_turn_id:string|null;attempt_id:string}|null
}
type WritingPending = { version:1; scope:NativeWritingScope; text_sha256:string; capture_action_id:string;
  answer_action_id:string; phase:'capture_unknown'|'captured'|'answer_unknown'|'answer_saved'|'resume_unknown'|'answered'|'capture_rejected'|'answer_rejected';
  original:NativeWritingOriginal|null; rejected_operations?:NativeWritingReceiptQuery[];
  resume?:NativeWritingResumeRequest; resume_operations?:NativeWritingResumeRequest[] }
const pendingPrefix = 'hsc-native-writing-v1:'
const pendingKey = (scope:NativeWritingScope):string => pendingPrefix + [scope.child_id,scope.session_id,scope.prompt_id].map(encodeURIComponent).join(':')
const pendingError = ():Error => Error('原稿提交结果待核对。可以询问刚才是否保存；不会重复采集、提交或评价。')
function readWritingPending(scope:NativeWritingScope):WritingPending|null {
  const raw=localStorage.getItem(pendingKey(scope))
  if(raw===null)return null
  let value:WritingPending
  try { value=JSON.parse(raw) } catch { throw pendingError() }
  if(!value || value.version!==1 || !value.scope || (!sameWritingScope(value.scope,scope)
      && !(value.phase==='capture_rejected' && pendingKey(value.scope)===pendingKey(scope)))
    || !/^[a-f0-9]{64}$/.test(value.text_sha256) || typeof value.capture_action_id!=='string'
    || !(value.capture_action_id===`native-writing:${value.scope.prompt_id}:${value.text_sha256}`
      || /^native-writing-retry:[a-f0-9-]{36}$/.test(value.capture_action_id))
    || typeof value.answer_action_id!=='string' || !['capture_unknown','captured','answer_unknown','answer_saved','resume_unknown','answered','capture_rejected','answer_rejected'].includes(value.phase)
    || value.resume && (!sameWritingScope(value.resume.target,value.scope) || value.resume.target.text_sha256!==value.text_sha256
      || value.resume.capture_action_id!==value.capture_action_id || value.resume.target.saved_answer?.answer_action_id!==value.answer_action_id
      || value.resume.client_action_id!==`native-writing-resume:${value.resume.authorized_turn_id}`)
    || JSON.stringify(value)!==raw)throw pendingError()
  return value
}
export type NativeWritingControlTarget=NativeWritingScope & {text_sha256:string; saved_answer?:{
  answer_action_id:string;original_artifact_id:string;original_revision:string;authorized_turn_id:string|null;payload_sha256:string
}}
export type NativeWritingResumeRequest={target:NativeWritingControlTarget;authorized_turn_id:string;client_action_id:string;capture_action_id:string}
export async function checkNativeWritingSubmission(childId:string, sessionId:string, isCurrent:()=>boolean,
  read:(query:NativeWritingReceiptQuery)=>Promise<NativeWritingReceipt>=readNativeWritingReceipt,
  target?:NativeWritingControlTarget):Promise<string|null> {
  const current=():void=>{if(!isCurrent())throw pendingError()}
  current()
  const prefix=pendingPrefix+[childId,sessionId].map(encodeURIComponent).join(':')+':'
  const entries=Object.keys(localStorage).filter(key=>key.startsWith(prefix)).reverse()
  for(const key of entries) {
    const value=JSON.parse(localStorage.getItem(key)??'null') as WritingPending|null
    if(!value?.scope || value.scope.child_id!==childId || value.scope.session_id!==sessionId || pendingKey(value.scope)!==key)throw pendingError()
    const prior=readWritingPending(value.scope)
    if(target && value.scope.prompt_id!==target.prompt_id)continue
    if(!prior || prior.phase==='answered' && !target)continue
    const receipt=await reconcileWritingPending(prior,read,current)
    current()
    if(receipt.state.endsWith('_rejected'))return '原操作已明确拒绝，没有保存该次提交；原编号和拒绝记录保留。修正后可以明确发起新的提交。'
    if(receipt.state==='answered')return '原稿和本题作答已核对保存；没有重新提交或启动评价，也不记作掌握。'
    if(receipt.state==='answer_saved')return '原稿和本题原回答已经保存，评分尚未完成。明确要求继续时可接续这份原回答；查询本身不会启动评分。'
    if(receipt.state==='captured' && ['capture_unknown','captured'].includes(prior.phase))return '原稿已核对保存，作答尚未提交。保留的文字无需重写；确认后可继续提交本题。'
    return '原稿提交的结果仍未确认，原编号已保留；没有重新采集、提交或评价。'
  }
  return '本会话没有待核对的原稿提交；没有重新采集、提交或评价。'
}
export async function retainedNativeWritingCurrent(draft:TypedWritingDraft|null,target:NativeWritingControlTarget,current:()=>boolean):Promise<boolean> {
  if(!draft || !current() || !sameWritingScope(draft.scope,target) || !draft.text.trim() || draft.text.trim().length>4000)return false
  const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(draft.text.trim()))))
    .map(v=>v.toString(16).padStart(2,'0')).join('')
  if(!current())return false
  if(hash!==target.text_sha256)throw Error('当前文字与已保存原稿不同，未覆盖旧稿或提交控制话。请先核对保留的原稿，修改须使用新的订正题。')
  const pending=readWritingPending(target)
  return Boolean(pending && pending.text_sha256===hash && ['captured','answer_rejected','answer_saved'].includes(pending.phase))
}
export type NativeWritingControl={target:NativeWritingControlTarget;turnId:string;isCurrent:()=>boolean;resolve:(message:string)=>void}
export const NATIVE_WRITING_CONTROL='hsc-native-writing-control'
export function dispatchRetainedNativeWriting(target:NativeWritingControlTarget,isCurrent:()=>boolean,turnId:string):Promise<string> {
  return new Promise(resolve=>{
    const fallback='保留原稿的提交结果尚未确认；没有把这句话当作答，也不会自动重发。'
    const timer=window.setTimeout(()=>finish(fallback),60000)
    let active=true
    const finish=(message:string):void=>{active=false;window.clearTimeout(timer);resolve(message)}
    const event=new CustomEvent<NativeWritingControl>(NATIVE_WRITING_CONTROL,{cancelable:true,
      detail:{target,turnId,isCurrent:()=>active&&isCurrent(),resolve:finish}})
    if(!isCurrent()){finish(fallback);return}
    window.dispatchEvent(event)
    if(!event.defaultPrevented)finish('本题的保留原稿还没有恢复，未提交作答。纸面原稿仍可按原流程核对。')
  })
}
function storeWritingPending(next:WritingPending, prior:WritingPending|null, current:()=>void):void {
  current()
  const key=pendingKey(next.scope), expected=prior===null?null:JSON.stringify(prior), raw=JSON.stringify(next)
  if(localStorage.getItem(key)!==expected)throw pendingError()
  localStorage.setItem(key,raw)
  current()
  if(localStorage.getItem(key)!==raw)throw pendingError()
}
function validOriginal(original:NativeWritingOriginal|null, scope:NativeWritingScope, hash:string):original is NativeWritingOriginal {
  return Boolean(original && sameWritingScope(original,scope) && original.text_sha256===hash && original.status==='completed'
    && original.local_only===true && original.cloud_transmission_allowed===false && original.artifact_id
    && /^[a-f0-9]{64}$/.test(original.original_revision))
}
const queryFor = (value:WritingPending):NativeWritingReceiptQuery => ({...value.scope,text_sha256:value.text_sha256,
  capture_action_id:value.capture_action_id,answer_action_id:value.answer_action_id,
  ...(value.resume?{resume_action_id:value.resume.client_action_id}:{})})
export async function readNativeWritingReceipt(query:NativeWritingReceiptQuery):Promise<NativeWritingReceipt> {
  return localApi.nativeWritingReceipt(query.session_id,query)
}
async function reconcileWritingPending(prior:WritingPending, read:(query:NativeWritingReceiptQuery)=>Promise<NativeWritingReceipt>,
  current:()=>void, rejectionOnly=false):Promise<NativeWritingReceipt> {
  current()
  const receipt=await read(queryFor(prior))
  current()
  if(!sameWritingScope(receipt,prior.scope) || receipt.text_sha256!==prior.text_sha256
    || receipt.capture_action_id!==prior.capture_action_id || receipt.answer_action_id!==prior.answer_action_id
    || receipt.local_only!==true || receipt.cloud_transmission_allowed!==false
    || !['capture_unknown','captured','answer_saved','answered','capture_rejected','answer_rejected'].includes(receipt.state)
    || localStorage.getItem(pendingKey(prior.scope))!==JSON.stringify(prior))throw pendingError()
  if(receipt.state==='capture_rejected' || receipt.state==='answer_rejected') {
    if(receipt.original!==null || receipt.result!==null
      || (receipt.state==='capture_rejected' ? Boolean(prior.answer_action_id || prior.original) : !prior.answer_action_id))throw pendingError()
    storeWritingPending({...prior,phase:receipt.state},prior,current)
    return receipt
  }
  if(rejectionOnly)return receipt
  if(receipt.state!=='capture_unknown' && !validOriginal(receipt.original,prior.scope,prior.text_sha256))throw pendingError()
  if(prior.original && (!receipt.original || prior.original.artifact_id!==receipt.original.artifact_id
    || prior.original.original_revision!==receipt.original.original_revision))throw pendingError()
  if(receipt.state==='answered') {
    const result=receipt.result, attempt=result?.attempt, prompt=result?.prompt
    if(!result || result.status!=='answered' || !attempt || !prompt || !sameWritingScope(writingScope(prompt),prior.scope)
      || attempt.child_id!==prior.scope.child_id || attempt.session_id!==prior.scope.session_id
      || attempt.diagnostic_item_id!==prior.scope.diagnostic_item_id || !attempt.attempt_id)throw pendingError()
    if(prior.resume && receipt.resume && (receipt.resume.client_action_id!==prior.resume.client_action_id
      || receipt.resume.authorized_turn_id!==prior.resume.authorized_turn_id
      || receipt.resume.original_authorized_turn_id!==prior.resume.target.saved_answer?.authorized_turn_id
      || receipt.resume.attempt_id!==attempt.attempt_id))throw pendingError()
  }
  const phase=receipt.state==='answered'?'answered':receipt.state==='answer_saved'?'answer_saved':prior.phase==='capture_unknown'&&receipt.state==='captured'?'captured':prior.phase
  const original=receipt.original??prior.original
  storeWritingPending({...prior,phase,original,answer_action_id:prior.answer_action_id || (original?`native-writing-answer:${original.artifact_id}`:'')},prior,current)
  return receipt
}
async function retainDefinitiveRejection(prior:WritingPending, read:(query:NativeWritingReceiptQuery)=>Promise<NativeWritingReceipt>,current:()=>void):Promise<void> {
  // Status codes are not proof: a 409 may follow a committed original/answer.
  const receipt=await reconcileWritingPending(prior,read,current,true).catch(()=>null)
  if(receipt?.state.endsWith('_rejected'))throw Error('原操作已明确拒绝，拒绝记录和原文保留。修正后可明确发起新的提交；没有自动重发。')
}
export async function submitNativeWriting(input: {
  scope: NativeWritingScope; text: string; isCurrent: () => boolean;
  capture: (input: NativeWritingInput) => Promise<NativeWritingOriginal>;
  answer: (values: {prompt_id:string;item_version:string;answer:string;client_action_id:string;
    writing_original_artifact_id:string;writing_original_revision:string}) => Promise<DiagnosticPromptResult>;
  read?: (query:NativeWritingReceiptQuery)=>Promise<NativeWritingReceipt>;
  resumeControl?:{target:NativeWritingControlTarget;turnId:string};
  resume?: (request:NativeWritingResumeRequest)=>Promise<NativeWritingReceipt>;
}): Promise<DiagnosticPromptResult & { native_writing_recovered?:boolean }> {
  input={...input,scope:{...input.scope}}
  const current = (): void => { if (!input.isCurrent()) throw Error('原稿仍保留在原题，当前孩子或题目已变化，没有提交到新题。') }
  current()
  const text = input.text.trim()
  if(!text || text.length>4000)throw Error('超出本次提交的 4000 字上限或原文为空。原文未截断。')
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text))))
    .map(v => v.toString(16).padStart(2,'0')).join('')
  current()
  let pending=readWritingPending(input.scope)
  let replaced:WritingPending|null=null
  let original:NativeWritingOriginal|undefined
  if(pending && (pending.phase==='capture_rejected' || pending.phase==='answer_rejected')) {
    const receipt=await reconcileWritingPending(pending,input.read??readNativeWritingReceipt,current)
    if(receipt.state!==pending.phase)throw pendingError()
    const history=[...(pending.rejected_operations??[]),queryFor(pending)]
    if(pending.phase==='capture_rejected') {
      replaced=pending
      pending=null
    } else {
      if(pending.text_sha256!==hash || !sameWritingScope(pending.scope,input.scope) || !validOriginal(pending.original,input.scope,hash))
        throw Error('这份原稿已经保存，不能用改写文字覆盖它。请保留原稿，在新的订正题中修改。')
      const retry:WritingPending={...pending,phase:'captured',answer_action_id:`native-writing-answer:${pending.original.artifact_id}:${crypto.randomUUID()}`,
        rejected_operations:history}
      storeWritingPending(retry,pending,current)
      // Only this explicit invocation may submit the retained original.
      original=pending.original
      pending=retry
    }
  }
  if(pending) {
    if(pending.text_sha256!==hash)throw Error('原稿提交尚未核对，修改后的文字没有覆盖或重新发送原操作。请先核对刚才的提交结果。')
    if(!original) {
      const receipt=await reconcileWritingPending(pending,input.read??readNativeWritingReceipt,current)
      if(receipt.state==='answered')return {...receipt.result!,native_writing_recovered:true}
      if(pending.phase==='answer_saved' && receipt.state==='answer_saved' && input.resumeControl) {
        const {target,turnId}=input.resumeControl, saved=target.saved_answer
        if(!turnId || !sameWritingScope(target,input.scope) || target.text_sha256!==hash || !saved
          || saved.answer_action_id!==pending.answer_action_id || saved.original_artifact_id!==receipt.original?.artifact_id
          || saved.original_revision!==receipt.original?.original_revision || pending.resume?.authorized_turn_id===turnId)throw pendingError()
        pending=readWritingPending(input.scope)!
        const request:NativeWritingResumeRequest={target,authorized_turn_id:turnId,client_action_id:`native-writing-resume:${turnId}`,
          capture_action_id:pending.capture_action_id}
        const sending:WritingPending={...pending,phase:'resume_unknown',resume:request,
          ...(pending.resume?{resume_operations:[...(pending.resume_operations??[]),pending.resume]}:{})}
        storeWritingPending(sending,pending,current)
        const resumed=await (input.resume??(request=>localApi.nativeWritingResume(input.scope.session_id,request)))(request)
        current()
        if(resumed.state!=='answered' || resumed.resume?.client_action_id!==request.client_action_id)throw pendingError()
        await reconcileWritingPending(sending,async()=>resumed,current)
        return resumed.result!
      }
      if(pending.phase!=='captured' || receipt.state!=='captured')throw pendingError()
      original=receipt.original!
      pending=readWritingPending(input.scope)!
    }
  } else {
    pending={version:1,scope:{...input.scope},text_sha256:hash,capture_action_id:replaced?`native-writing-retry:${crypto.randomUUID()}`:`native-writing:${input.scope.prompt_id}:${hash}`,
      answer_action_id:'',phase:'capture_unknown',original:null,
      ...(replaced?{rejected_operations:[...(replaced.rejected_operations??[]),queryFor(replaced)]}:{})}
    storeWritingPending(pending,replaced,current)
    try {
      original=await input.capture({...input.scope,text,client_action_id:pending.capture_action_id})
    } catch(error) {
      await retainDefinitiveRejection(pending,input.read??readNativeWritingReceipt,current)
      throw error
    }
    current()
    if(!validOriginal(original,input.scope,hash))throw Error('原稿回执不属于当前题目或这份文字，未借用其他稿件。')
    const captured:WritingPending={...pending,phase:'captured',original,answer_action_id:`native-writing-answer:${original.artifact_id}`}
    storeWritingPending(captured,pending,current);pending=captured
  }
  const sending:WritingPending={...pending,phase:'answer_unknown'}
  storeWritingPending(sending,pending,current)
  let result:DiagnosticPromptResult
  try {
    result=await input.answer({prompt_id:input.scope.prompt_id,item_version:input.scope.item_version,answer:text,
      client_action_id:sending.answer_action_id,writing_original_artifact_id:original.artifact_id,
      writing_original_revision:original.original_revision})
  } catch(error) {
    await retainDefinitiveRejection(sending,input.read??readNativeWritingReceipt,current)
    throw error
  }
  current()
  if(localStorage.getItem(pendingKey(input.scope))!==JSON.stringify(sending))throw pendingError()
  if(result.attempt && (!result.prompt || !sameWritingScope(writingScope(result.prompt),input.scope)
    || result.attempt.child_id!==input.scope.child_id || result.attempt.session_id!==input.scope.session_id
    || result.attempt.diagnostic_item_id!==input.scope.diagnostic_item_id))throw pendingError()
  return result
}

export type NativeWritingFeedback = {
  scope: NativeWritingScope; result: DiagnosticPromptResult; isCurrent: () => boolean; resolve: (message:string) => void
}
export const NATIVE_WRITING_FEEDBACK = 'hsc-native-writing-feedback'
export function presentNativeWritingFeedback(scope:NativeWritingScope,result:DiagnosticPromptResult,isCurrent:()=>boolean):Promise<string> {
  return new Promise(resolve => {
    const fallback='原稿已保存，反馈尚未确认；不会重复提交或记作掌握。'
    const timer=window.setTimeout(()=>resolve(fallback),60000)
    const event=new CustomEvent<NativeWritingFeedback>(NATIVE_WRITING_FEEDBACK,{cancelable:true,detail:{scope,result,isCurrent,
      resolve:message=>{window.clearTimeout(timer);resolve(message)}}})
    window.dispatchEvent(event)
    if(!event.defaultPrevented){window.clearTimeout(timer);resolve(fallback)}
  })
}
