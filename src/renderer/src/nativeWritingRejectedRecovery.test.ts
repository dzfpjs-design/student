import { webcrypto } from 'node:crypto'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { submitNativeWriting, type NativeWritingReceiptQuery, type NativeWritingReceipt } from './nativeWritingFlow'
import type { NativeWritingInput, NativeWritingOriginal } from '../../shared/nativeWriting'

const scope={child_id:'child',session_id:'session',prompt_id:'prompt',item_version:'version',diagnostic_item_id:'item'}
beforeEach(()=>{localStorage.clear();vi.stubGlobal('crypto',webcrypto)})
afterEach(()=>{localStorage.clear();vi.restoreAllMocks();vi.unstubAllGlobals()})
const rejected=(q:NativeWritingReceiptQuery,stage:string)=>({...q,state:stage+'_rejected',original:null,result:null,
  local_only:true,cloud_transmission_allowed:false}) as NativeWritingReceipt
async function original(input:NativeWritingInput):Promise<NativeWritingOriginal> {
  return {...scope,artifact_id:'original',original_revision:'a'.repeat(64),
    text_sha256:Buffer.from(await webcrypto.subtle.digest('SHA-256',Buffer.from(input.text))).toString('hex'),
    status:'completed',local_only:true,cloud_transmission_allowed:false}
}
it('a confirmed capture rejection survives reload and permits explicit corrected text with a new identity and history',async()=>{
  const capture=vi.fn().mockRejectedValueOnce(Error('signed409')).mockImplementation(original)
  const read=vi.fn(async(q:NativeWritingReceiptQuery)=>rejected(q,'capture'))
  const answer=vi.fn().mockResolvedValue({status:'waiting_answer',attempt:null})
  await expect(submitNativeWriting({scope,text:'Original text',capture,answer,read,isCurrent:()=>true})).rejects.toThrow()
  expect(JSON.stringify(localStorage)).toContain('capture_rejected')
  expect(capture).toHaveBeenCalledTimes(1);expect(answer).not.toHaveBeenCalled()
  vi.resetModules()
  const reopened=await import('./nativeWritingFlow')
  await reopened.submitNativeWriting({scope,text:'Corrected text',capture,answer,read,isCurrent:()=>true})
  expect(capture).toHaveBeenCalledTimes(2)
  expect(capture.mock.calls[0][0].client_action_id).not.toBe(capture.mock.calls[1][0].client_action_id)
  expect(JSON.stringify(localStorage)).toContain(capture.mock.calls[0][0].client_action_id)
  expect(JSON.stringify(localStorage)).not.toContain('Original text')
  expect(answer.mock.calls[0][0].answer).toBe('Corrected text')
})
it('a confirmed answer rejection permits only an explicit new answer identity, without recapturing the original',async()=>{
  const capture=vi.fn(original),answer=vi.fn().mockRejectedValueOnce(Error('409')).mockResolvedValue({status:'waiting_answer',attempt:null})
  const read=vi.fn(async(q:NativeWritingReceiptQuery)=>rejected(q,'answer'))
  const input={scope,text:'Original text',capture,answer,read,isCurrent:()=>true}
  await expect(submitNativeWriting(input)).rejects.toThrow()
  expect(JSON.stringify(localStorage)).toContain('answer_rejected')
  await submitNativeWriting(input)
  expect(capture).toHaveBeenCalledTimes(1)
  expect(answer).toHaveBeenCalledTimes(2)
  expect(answer.mock.calls[0][0].client_action_id).not.toBe(answer.mock.calls[1][0].client_action_id)
  expect(JSON.stringify(localStorage)).toContain(answer.mock.calls[0][0].client_action_id)
})
it('an ambiguous failure with absent receipt remains unknown and blocks corrected text without any repost',async()=>{
  const capture=vi.fn().mockRejectedValue(Error('transport503')),answer=vi.fn()
  const read=vi.fn(async(q:NativeWritingReceiptQuery)=>({...q,state:'capture_unknown',original:null,result:null,
    local_only:true,cloud_transmission_allowed:false}) as NativeWritingReceipt)
  await submitNativeWriting({scope,text:'Original text',capture,answer,read,isCurrent:()=>true}).catch(()=>undefined)
  await submitNativeWriting({scope,text:'Changed text',capture,answer,read,isCurrent:()=>true}).catch(()=>undefined)
  expect(JSON.stringify(localStorage)).toContain('capture_unknown')
  expect(capture).toHaveBeenCalledTimes(1);expect(answer).not.toHaveBeenCalled()
})
it.each(['scope','new_owner','privacy'])('late rejection read cannot replace a changed operation (%s)',async fault=>{
  let current=true
  const capture=vi.fn().mockRejectedValue(Error('409')),answer=vi.fn()
  const read=async(q:NativeWritingReceiptQuery)=>{
    if(fault==='new_owner')localStorage.setItem(Object.keys(localStorage)[0],'newer independent owner')
    if(fault==='privacy')current=false
    return rejected(fault==='scope'?{...q,child_id:'other-child'}:q,'capture')
  }
  await submitNativeWriting({scope,text:'Original text',capture,answer,read,isCurrent:()=>current}).catch(()=>undefined)
  expect(JSON.stringify(localStorage)).not.toContain('capture_rejected')
  expect(capture).toHaveBeenCalledTimes(1);expect(answer).not.toHaveBeenCalled()
  if(fault==='new_owner')expect(Object.values(localStorage)).toContain('newer independent owner')
})
it('silent history replacement loss prevents the corrected capture POST',async()=>{
  const capture=vi.fn().mockRejectedValue(Error('409')),answer=vi.fn()
  const read=async(q:NativeWritingReceiptQuery)=>rejected(q,'capture')
  const input={scope,text:'Original text',capture,answer,read,isCurrent:()=>true}
  await submitNativeWriting(input).catch(()=>undefined)
  const before=JSON.stringify(localStorage)
  vi.spyOn(Storage.prototype,'setItem').mockImplementation(()=>undefined)
  await submitNativeWriting({...input,text:'Corrected text'}).catch(()=>undefined)
  expect(JSON.stringify(localStorage)).toBe(before)
  expect(capture).toHaveBeenCalledTimes(1)
})
