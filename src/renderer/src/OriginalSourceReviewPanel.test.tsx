import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { OriginalSourceReviewPanel, originalReviewKey, type OriginalSourceView } from './OriginalSourceReviewPanel'

afterEach(()=>{cleanup();localStorage.clear();vi.restoreAllMocks()})
const original='合成原题：先算括号，再乘三。'
const view=(overrides={}):OriginalSourceView=>({child_id:'one',commitment_id:'work',version:'v1',status:'mapping_required',
  original_question:original,subject:'数学',references:[{item_id:'ref',item_version:'i1',node_id:'node',target:'运算顺序',question:'合成参考题：先加再乘。'}],
  truncated:false,mapping:null,local_only:true,cloud_transmission_allowed:false,mastery_update_allowed:false,...overrides})
const props=(request:ReturnType<typeof vi.fn>)=>({childId:'one',commitmentId:'work',request:request as never,onAuthNeeded:vi.fn(),onClose:vi.fn(),onSaved:vi.fn()})
function fill():void {
  fireEvent.change(screen.getByLabelText('参考题与目标'),{target:{value:'ref'}})
  fireEvent.change(screen.getByLabelText('原题中的依据'),{target:{value:'先算括号'}})
  fireEvent.change(screen.getByLabelText('审核依据与备注'),{target:{value:'原题和参考题都要求先算括号，不能当作掌握证据。'}})
}
function stored(){return JSON.parse(localStorage.getItem(originalReviewKey('one','work'))!)}
function result(command:ReturnType<typeof stored>['pending'],overrides={}) {
  return {receipt:{child_id:'one',commitment_id:'work',action_id:command.action_id,decision:'approved',revision:1},
    current:view({version:'v2',status:'ready',mapping:{decision:'approved',note:command.note}}),...overrides}
}
it('shows original evidence and saves exact action before POST; current saved state replaces historic receipt',async()=>{
  const request=vi.fn().mockImplementation(async(_path:string,init?:RequestInit)=>{
    if(!init)return view()
    const command=JSON.parse(init.body as string)
    expect(stored().pending).toEqual(command)
    expect(command.reference_quote).toBe('运算顺序')
    return result(command)
  })
  const p=props(request);render(<OriginalSourceReviewPanel {...p}/>)
  await screen.findByText(original);fill()
  fireEvent.click(screen.getByRole('button',{name:'确认目标关联'}))
  await screen.findByText(/原操作已确认保存/)
  expect(stored().pending).toBeNull()
  expect(screen.getByText(/等待孩子语音发起检查/)).toBeInTheDocument()
  expect(p.onSaved).toHaveBeenCalledOnce()
})
it('retains unknown operation across remount; only explicit GET confirms it, never another POST',async()=>{
  const request=vi.fn().mockImplementation(async(path:string,init?:RequestInit)=>{
    if(init)throw Error('reply lost')
    if(path.includes('/receipts/'))return result(stored().pending)
    return view()
  })
  const p=props(request),mounted=render(<OriginalSourceReviewPanel {...p}/>)
  await screen.findByText(original);fill();fireEvent.click(screen.getByRole('button',{name:'确认目标关联'}))
  await screen.findByText(/操作结果待确认/)
  const pending=stored().pending
  mounted.unmount();render(<OriginalSourceReviewPanel {...p}/>)
  await screen.findByText(original)
  expect(screen.getByLabelText('审核依据与备注')).toHaveValue(pending.note)
  expect(request.mock.calls.filter(c=>c[1]?.method==='POST')).toHaveLength(1)
  fireEvent.click(screen.getByRole('button',{name:'核对原提交结果'}))
  await waitFor(()=>expect(stored().pending).toBeNull())
  expect(request).toHaveBeenLastCalledWith('/v1/parent/children/one/original-checks/work/receipts/'+pending.action_id)
  expect(request.mock.calls.filter(c=>c[1]?.method==='POST')).toHaveLength(1)
})
it('rejects a receipt for another child and retains the exact original request',async()=>{
  const request=vi.fn().mockImplementation(async(_path:string,init?:RequestInit)=>init?
    result(stored().pending,{receipt:{child_id:'two',commitment_id:'work',action_id:stored().pending.action_id,decision:'approved',revision:1}}):view())
  render(<OriginalSourceReviewPanel {...props(request)}/>);await screen.findByText(original);fill()
  fireEvent.click(screen.getByRole('button',{name:'确认目标关联'}))
  await screen.findByText(/操作结果待确认/);expect(stored().pending).not.toBeNull()
})
it('privacy deletion invalidates pending responses without restoring local or visible data',async()=>{
  let resolve!:(value:unknown)=>void
  const request=vi.fn().mockImplementation(async(_path:string,init?:RequestInit)=>init?new Promise(r=>{resolve=r}):view())
  const p=props(request);render(<OriginalSourceReviewPanel {...p}/>);await screen.findByText(original);fill()
  fireEvent.click(screen.getByRole('button',{name:'确认目标关联'}))
  const response=result(stored().pending)
  localStorage.removeItem(originalReviewKey('one','work'))
  act(()=>window.dispatchEvent(new CustomEvent('hsc-original-source-review-cleared',{detail:'one'})))
  await act(async()=>resolve(response))
  expect(screen.queryByText(original)).not.toBeInTheDocument()
  expect(localStorage.getItem(originalReviewKey('one','work'))).toBeNull()
  expect(p.onSaved).not.toHaveBeenCalled()
  expect(screen.getByLabelText('审核依据与备注')).toHaveValue('')
})
it('keeps typed words on storage failure but sends no request',async()=>{
  const request=vi.fn().mockResolvedValue(view())
  render(<OriginalSourceReviewPanel {...props(request)}/>);await screen.findByText(original)
  const save=vi.spyOn(Storage.prototype,'setItem').mockImplementation(()=>{throw Error('full')})
  fireEvent.change(screen.getByLabelText('审核依据与备注'),{target:{value:'这一段人工判断不能丢失'}})
  expect(screen.getByLabelText('审核依据与备注')).toHaveValue('这一段人工判断不能丢失')
  expect(screen.getByRole('button',{name:'确认目标关联'})).toBeDisabled()
  expect(request).toHaveBeenCalledTimes(1)
  save.mockRestore();fireEvent.click(screen.getByRole('button',{name:'重试保存本机草稿'}))
  expect(stored().note).toBe('这一段人工判断不能丢失')
  expect(request).toHaveBeenCalledTimes(1)
})
it('read failure disables approval; changed version retains notes and requires explicit rebase',async()=>{
  const request=vi.fn().mockResolvedValue(view())
  render(<OriginalSourceReviewPanel {...props(request)}/>);await screen.findByText(original);fill()
  request.mockRejectedValueOnce(Error('offline'))
  fireEvent.click(screen.getByRole('button',{name:'刷新原题来源'}));await screen.findByText(/原题来源未能核对/)
  expect(screen.getByRole('button',{name:'确认目标关联'})).toBeDisabled()
  request.mockResolvedValue(view({version:'v2'}))
  fireEvent.click(screen.getByRole('button',{name:'刷新原题来源'}));await screen.findByText(/来源版本已变化/)
  expect(screen.getByRole('button',{name:'确认目标关联'})).toBeDisabled()
  fireEvent.click(screen.getByRole('button',{name:'核对当前来源，重新选题'}))
  expect(stored().version).toBe('v2');expect(stored().item).toBe('');expect(stored().note).toContain('不能当作掌握证据')
})
it('does not render a late response for the previous child',async()=>{
  let resolve!:(value:OriginalSourceView)=>void
  const request=vi.fn().mockImplementationOnce(()=>new Promise(r=>{resolve=r})).mockResolvedValue(view({child_id:'two',original_question:'第二个孩子的合成题'}))
  const p=props(request),mounted=render(<OriginalSourceReviewPanel {...p}/>)
  mounted.rerender(<OriginalSourceReviewPanel {...p} childId="two"/>)
  await screen.findByText('第二个孩子的合成题')
  await act(async()=>resolve(view()))
  expect(screen.queryByText(original)).not.toBeInTheDocument()
  expect(localStorage.getItem(originalReviewKey('one','work'))).toBeNull()
})
it('only a verified missing receipt with newer source allows explicit release of the old request',async()=>{
  const request=vi.fn().mockImplementation(async(path:string,init?:RequestInit)=>{
    if(init)throw Error('unknown')
    return path.includes('/receipts/')?{receipt:null,current:view({version:'v2'})}:view()
  })
  render(<OriginalSourceReviewPanel {...props(request)}/>);await screen.findByText(original);fill()
  fireEvent.click(screen.getByRole('button',{name:'确认目标关联'}));await screen.findByText(/操作结果待确认/)
  expect(screen.queryByRole('button',{name:'放弃未落库旧请求，重新审核'})).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'核对原提交结果'}))
  fireEvent.click(await screen.findByRole('button',{name:'放弃未落库旧请求，重新审核'}))
  expect(stored().pending).toBeNull();expect(stored().version).toBe('v2');expect(stored().item).toBe('')
  expect(stored().note).toContain('不能当作掌握证据')
  expect(request.mock.calls.filter(c=>c[1]?.method==='POST')).toHaveLength(1)
})
