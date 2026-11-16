import { webcrypto } from 'node:crypto'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { submitNativeWriting } from './nativeWritingFlow'
import type { NativeWritingInput, NativeWritingOriginal } from '../../shared/nativeWriting'
const scope = { child_id:'child', session_id:'session', prompt_id:'prompt', item_version:'version', diagnostic_item_id:'item' }
beforeEach(() => { localStorage.clear(); vi.stubGlobal('crypto', webcrypto) })
afterEach(() => { localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
async function original(input: NativeWritingInput): Promise<NativeWritingOriginal> {
  return { ...scope, artifact_id:'exact-original', original_revision:'a'.repeat(64),
    text_sha256:Buffer.from(await webcrypto.subtle.digest('SHA-256', Buffer.from(input.text))).toString('hex'),
    status:'completed', local_only:true, cloud_transmission_allowed:false }
}
it.each(['capture','answer'])('after %s response loss and module restart, the original operation is read without repeating either POST', async lost => {
  let saved: NativeWritingOriginal | null = null
  const capture = vi.fn(async (input: NativeWritingInput) => { saved = await original(input); if (lost === 'capture') throw Error('lost capture receipt'); return saved })
  const answer = vi.fn(async () => { throw Error('lost answer receipt') })
  const read = vi.fn(async (query:import('./nativeWritingFlow').NativeWritingReceiptQuery):Promise<import('./nativeWritingFlow').NativeWritingReceipt> =>
    ({...query, state:lost === 'capture' ? 'captured' : 'answer_saved', original:saved, result:null,local_only:true,cloud_transmission_allowed:false}))
  const input = { scope, text:'Original manuscript', isCurrent:()=>true, capture, answer, read }
  await expect(submitNativeWriting(input)).rejects.toThrow()
  vi.resetModules()
  const reopened = await import('./nativeWritingFlow')
  await reopened.submitNativeWriting(input).catch(() => undefined)
  expect(read).toHaveBeenCalledTimes(2)
  expect(capture).toHaveBeenCalledTimes(1)
  expect(answer).toHaveBeenCalledTimes(lost === 'capture' ? 0 : 1)
})
it('silent pending journal loss prevents capture POST', async () => {
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => undefined)
  const capture = vi.fn(original), answer = vi.fn().mockResolvedValue({ status:'answered' })
  await submitNativeWriting({ scope, text:'Original manuscript', isCurrent:()=>true, capture, answer }).catch(() => undefined)
  expect(capture).not.toHaveBeenCalled(); expect(answer).not.toHaveBeenCalled()
})
it('changed text cannot overwrite an unresolved original operation after reload', async () => {
  const capture = vi.fn(async () => { throw Error('capture response lost') }), answer = vi.fn()
  const input = { scope, text:'Original manuscript', isCurrent:()=>true, capture, answer }
  await submitNativeWriting(input).catch(() => undefined)
  await submitNativeWriting({ ...input, text:'Changed manuscript' }).catch(() => undefined)
  expect(capture).toHaveBeenCalledTimes(1)
})
it('retains only scope hashes and action IDs, never the manuscript text', async () => {
  await submitNativeWriting({scope,text:'Private manuscript must not be journaled.',isCurrent:()=>true,
    capture:async()=>{throw Error('lost')},answer:vi.fn()}).catch(()=>undefined)
  expect(JSON.stringify(localStorage)).not.toContain('Private manuscript')
  expect(JSON.stringify(localStorage)).toContain('capture_unknown')
})
it.each(['item_version','diagnostic_item_id'])('scope change %s under the same prompt cannot replace an unknown operation', async field => {
  const capture=vi.fn(async()=>{throw Error('lost')}),answer=vi.fn()
  await submitNativeWriting({scope,text:'Original manuscript',isCurrent:()=>true,capture,answer}).catch(()=>undefined)
  await submitNativeWriting({scope:{...scope,[field]:'changed'},text:'Original manuscript',isCurrent:()=>true,capture,answer}).catch(()=>undefined)
  expect(capture).toHaveBeenCalledTimes(1);expect(answer).not.toHaveBeenCalled()
})
it('late capture cannot overwrite a newer owner or start an answer', async () => {
  const answer=vi.fn()
  await expect(submitNativeWriting({scope,text:'Original manuscript',isCurrent:()=>true,capture:async input=>{
    const key=Object.keys(localStorage).find(key=>key.startsWith('hsc-native-writing-v1:'))!
    localStorage.setItem(key,'new independent owner')
    return original(input)
  },answer})).rejects.toThrow()
  expect(Object.values(localStorage)).toContain('new independent owner');expect(answer).not.toHaveBeenCalled()
})
it('late answer after a child/scope change never returns feedback or removes the durable pending identity', async () => {
  let current=true
  await expect(submitNativeWriting({scope,text:'Original manuscript',isCurrent:()=>current,capture:original,
    answer:async()=>{current=false;return {status:'answered',prompt:null,attempt:null,reason:null}}})).rejects.toThrow('当前孩子或题目已变化')
  expect(JSON.stringify(localStorage)).toContain('answer_unknown')
})
it('a second child has a separate identity and cannot read or overwrite the first child operation', async () => {
  const capture=vi.fn(async()=>{throw Error('lost')}),answer=vi.fn()
  await submitNativeWriting({scope,text:'First manuscript',isCurrent:()=>true,capture,answer}).catch(()=>undefined)
  const first=JSON.stringify(localStorage)
  await submitNativeWriting({scope:{...scope,child_id:'other-child',session_id:'other-session'},text:'Second manuscript',isCurrent:()=>true,capture,answer}).catch(()=>undefined)
  expect(Object.keys(localStorage).filter(k=>k.startsWith('hsc-native-writing-v1:'))).toHaveLength(2)
  expect(localStorage.getItem(Object.keys(JSON.parse(first))[0])).toBe(Object.values(JSON.parse(first))[0])
  expect(capture).toHaveBeenCalledTimes(2);expect(answer).not.toHaveBeenCalled()
})
