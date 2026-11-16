import {webcrypto} from 'node:crypto'
import {beforeEach,afterEach,it,expect,vi} from 'vitest'
import {checkNativeWritingSubmission,submitNativeWriting,type NativeWritingReceipt,type NativeWritingReceiptQuery,type NativeWritingControlTarget} from './nativeWritingFlow'
import type {NativeWritingOriginal} from '../../shared/nativeWriting'
import type {DiagnosticPromptResult} from './types'

const scope={child_id:'child',session_id:'session',prompt_id:'prompt',item_version:'v1',diagnostic_item_id:'item'}
const text='The retained typed original.'
beforeEach(()=>{localStorage.clear();vi.stubGlobal('crypto',webcrypto)})
afterEach(()=>{localStorage.clear();vi.restoreAllMocks();vi.unstubAllGlobals()})
async function setup(){
  const hash=Buffer.from(await webcrypto.subtle.digest('SHA-256',Buffer.from(text))).toString('hex')
  const original:NativeWritingOriginal={...scope,text_sha256:hash,original_revision:'a'.repeat(64),artifact_id:'original',status:'completed',local_only:true,cloud_transmission_allowed:false}
  const capture=vi.fn(async()=>original),answer=vi.fn(async()=>{throw Error('after-save reply lost')})
  let answered=false
  const result={status:'answered',prompt:{...scope,origin_session_id:scope.session_id},attempt:{attempt_id:'exact-attempt',
    child_id:scope.child_id,session_id:scope.session_id,diagnostic_item_id:scope.diagnostic_item_id,submitted_answer:text}} as unknown as DiagnosticPromptResult
  const read=vi.fn(async(q:NativeWritingReceiptQuery):Promise<NativeWritingReceipt>=>({...q,
    state:answered?'answered':'answer_saved',original,result:answered?result:null,local_only:true,cloud_transmission_allowed:false}))
  const input={scope,text,isCurrent:()=>true,capture,answer,read}
  await submitNativeWriting(input).catch(()=>undefined)
  const target:NativeWritingControlTarget={...scope,text_sha256:hash,saved_answer:{answer_action_id:'native-writing-answer:original',
    original_artifact_id:'original',original_revision:original.original_revision,authorized_turn_id:'original-turn',payload_sha256:'b'.repeat(64)}}
  const resume=vi.fn(async(request):Promise<NativeWritingReceipt>=>{
    answered=true
    return {...scope,text_sha256:hash,capture_action_id:`native-writing:${scope.prompt_id}:${hash}`,
      answer_action_id:target.saved_answer!.answer_action_id,state:'answered',original,result,local_only:true,cloud_transmission_allowed:false,
      resume:{client_action_id:request.client_action_id,authorized_turn_id:request.authorized_turn_id,original_authorized_turn_id:'original-turn',attempt_id:'exact-attempt'}}
  })
  return {input,target,resume,read,capture,answer,result,original}
}
it('unknown answer only reads; a later explicit admitted control uses exact resume, never answer',async()=>{
  const x=await setup()
  await expect(submitNativeWriting({...x.input,resume:x.resume,resumeControl:{target:x.target,turnId:'query-time-turn'}})).rejects.toThrow()
  expect(x.resume).not.toHaveBeenCalled()
  expect(JSON.stringify(localStorage)).toContain('answer_saved')
  await submitNativeWriting({...x.input,resume:x.resume,resumeControl:{target:x.target,turnId:'fresh-explicit-turn'}})
  expect(x.capture).toHaveBeenCalledTimes(1);expect(x.answer).toHaveBeenCalledTimes(1)
  expect(x.resume).toHaveBeenCalledWith({target:x.target,authorized_turn_id:'fresh-explicit-turn',
    client_action_id:'native-writing-resume:fresh-explicit-turn',capture_action_id:`native-writing:prompt:${x.target.text_sha256}`})
  expect(JSON.stringify(localStorage)).not.toContain(text)
})
it('query and ordinary submit without explicit control never resume a saved answer',async()=>{
  const x=await setup()
  expect(await checkNativeWritingSubmission('child','session',()=>true,x.read)).toContain('原回答已经保存')
  await submitNativeWriting({...x.input,resume:x.resume}).catch(()=>undefined)
  expect(x.resume).not.toHaveBeenCalled();expect(x.answer).toHaveBeenCalledTimes(1)
})
it('lost resume reply survives module restart and is recovered by exact GET without another POST',async()=>{
  const x=await setup()
  await checkNativeWritingSubmission('child','session',()=>true,x.read)
  const lost=vi.fn(async request=>{await x.resume(request);throw Error('postcommit 503')})
  await submitNativeWriting({...x.input,resume:lost,resumeControl:{target:x.target,turnId:'explicit-resume'}}).catch(()=>undefined)
  const before=JSON.stringify(localStorage)
  expect(before).toContain('resume_unknown');expect(before).toContain('original-turn')
  vi.resetModules()
  const reopened=await import('./nativeWritingFlow')
  const recovered=await reopened.submitNativeWriting({...x.input,resume:lost,resumeControl:{target:x.target,turnId:'later-query'}})
  expect(recovered.native_writing_recovered).toBe(true)
  expect(lost).toHaveBeenCalledTimes(1);expect(x.answer).toHaveBeenCalledTimes(1)
  expect(x.read.mock.calls.at(-1)![0].resume_action_id).toBe('native-writing-resume:explicit-resume')
})
it.each(['child','text','revision','key','silent_storage','late_owner','privacy'] as const)('saved resume fails closed (%s)',async fault=>{
  const x=await setup()
  await checkNativeWritingSubmission('child','session',()=>true,x.read)
  const target=structuredClone(x.target)
  if(fault==='child')target.child_id='other-child'
  if(fault==='text')target.text_sha256='f'.repeat(64)
  if(fault==='revision')target.saved_answer!.original_revision='f'.repeat(64)
  if(fault==='key')target.saved_answer!.answer_action_id='another-answer'
  if(fault==='silent_storage')vi.spyOn(Storage.prototype,'setItem').mockImplementation(()=>undefined)
  let current=true
  const read=vi.fn(async q=>{
    const result=await x.read(q)
    if(fault==='late_owner')localStorage.setItem(Object.keys(localStorage)[0],'new owner')
    if(fault==='privacy')current=false
    return result
  })
  await expect(submitNativeWriting({...x.input,read,isCurrent:()=>current,resume:x.resume,resumeControl:{target,turnId:'explicit-turn'}})).rejects.toThrow()
  expect(x.resume).not.toHaveBeenCalled();expect(x.capture).toHaveBeenCalledTimes(1);expect(x.answer).toHaveBeenCalledTimes(1)
  if(fault==='late_owner')expect(Object.values(localStorage)).toContain('new owner')
})
