import {webcrypto} from 'node:crypto'
import {beforeEach,expect,it,vi} from 'vitest'
import {submitNativeWriting,isWrittenDiagnostic,isSavedNativeWritingPrompt} from './nativeWritingFlow'
import type {DiagnosticItem,DiagnosticPromptResult} from './types'
import type {NativeWritingInput,NativeWritingOriginal} from '../../shared/nativeWriting'
const scope={child_id:'child',session_id:'session',prompt_id:'prompt',item_version:'version',diagnostic_item_id:'item'}
beforeEach(()=>{localStorage.clear();vi.stubGlobal('crypto',webcrypto)})
it.each(['written','spoken_explanation',null,undefined] as const)('restores open-response manuscripts only for a confirmed written mode (%s)',mode=>{
  const item:DiagnosticItem={diagnostic_item_id:scope.diagnostic_item_id,knowledge_node_id:'node',track:'school',
    subject:'数学',title:'说明思路',prompt:'请说明理由。',difficulty:2,
    answer_type:'open_response',assessment_mode:'open_response_rubric',response_mode:mode}
  const result:DiagnosticPromptResult={status:'answer_saved',attempt:null,reason:null,
    prompt:{...scope,origin_session_id:scope.session_id,review_task_id:null,created_at:'2026-09-13T00:00:00Z',item}}
  const draft={input_kind:'typed' as const,scope,text:'Retained original'}
  expect(isWrittenDiagnostic(item)).toBe(mode==='written')
  expect(isSavedNativeWritingPrompt(result,draft)).toBe(mode==='written')
  expect(isSavedNativeWritingPrompt(result,{...draft,scope:{...scope,child_id:'another-child'}})).toBe(false)
})
async function original(input:NativeWritingInput):Promise<NativeWritingOriginal>{
  const h=Buffer.from(await webcrypto.subtle.digest('SHA-256',Buffer.from(input.text))).toString('hex')
  return {...scope,artifact_id:'exact-native-original',original_revision:'a'.repeat(64),text_sha256:h,status:'completed',local_only:true,cloud_transmission_allowed:false}
}
it('passes only the exact native receipt, keeps the post-commit feedback notice',async()=>{
  const answer=vi.fn().mockResolvedValue({local_learning_evaluation:{status:'feedback_required'}})
  const result=await submitNativeWriting({scope,text:'Child original',isCurrent:()=>true,capture:original,answer})
  expect(answer).toHaveBeenCalledWith(expect.objectContaining({writing_original_artifact_id:'exact-native-original',writing_original_revision:'a'.repeat(64)}))
  expect(result.local_learning_evaluation?.status).toBe('feedback_required')
})
it.each(['child_id','session_id','prompt_id','item_version','diagnostic_item_id','text_sha256','original_revision','status'])('rejects mismatched %s without answer submission',async field=>{
  const answer=vi.fn()
  await expect(submitNativeWriting({scope,text:'Child original',isCurrent:()=>true,
    capture:async input=>({...await original(input),[field]:'other'}),answer})).rejects.toThrow('原稿回执')
  expect(answer).not.toHaveBeenCalled()
})
it('does not submit a late native capture after a child or prompt switch',async()=>{
  let current=true;const answer=vi.fn()
  await expect(submitNativeWriting({scope,text:'Child original',isCurrent:()=>current,
    capture:async input=>{current=false;return original(input)},answer})).rejects.toThrow('当前孩子或题目已变化')
  expect(answer).not.toHaveBeenCalled()
})
