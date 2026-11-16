import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { BehaviorGoalReviewPanel } from './BehaviorGoalReviewPanel'
import { behaviorGoalLocalKey, clearBehaviorGoalLocalState } from './behaviorGoalApi'
import type { BehaviorGoalCommand, BehaviorGoalRequest } from './behaviorGoalApi'
import { goalView } from './BehaviorGoalReviewPanel.fixtures'

const props={childId:'a',experimentId:'goal',title:'先指出卡在哪一步',childAction:'圈出不确定的地方后再求助',successSignal:'明确问题后再求助'}
function fixture() {
  let view=goalView(), lose=false, failRead=false, modelFail=false
  const receipts=new Map<string,string>()
  const request=vi.fn<BehaviorGoalRequest>(async(path,init)=>{
    if(path.endsWith('/longitudinal-model')) {
      if(modelFail) throw new TypeError('model unavailable')
      return {child_id:'a',is_current:true,evidence_fingerprint:'current-model',payload:{behavior:{}}}
    }
    if(init?.method!=='POST') { if(failRead) throw new TypeError('offline'); return structuredClone(view) }
    const body=JSON.parse(String(init.body)) as BehaviorGoalCommand
    if(!receipts.has(body.client_action_id)) {
      if(body.expected_version!==view.opportunities[0].binding_version) throw new ApiError('changed',409)
      receipts.set(body.client_action_id,String(init.body))
      const {client_action_id:_,...original}=body
      view.opportunities[0].confirmation=body.operation==='retract'?null:{...original,request:original,event_id:'event',occurred_at:'2026-09-08T01:00:00Z',actor_type:'parent'}
      view.opportunities[0].binding_version=(body.operation==='confirm'?'d':'e').repeat(64)
    } else expect(receipts.get(body.client_action_id)).toBe(init.body)
    if(lose) {lose=false;throw new TypeError('lost response')}
    return {status:'recorded',event_id:'event',replayed:true,historical_receipt:true,local_only:true,cloud_transmission_allowed:false}
  })
  return {request, read:()=>view, set:(v:ReturnType<typeof goalView>)=>{view=v},lose:()=>{lose=true},failRead:()=>{failRead=true},modelFail:()=>{modelFail=true},posts:()=>request.mock.calls.filter(([,i])=>i?.method==='POST')}
}
async function open() {
  fireEvent.click(screen.getByText('核对具体机会'))
  await screen.findByLabelText('选择需要补充的机会')
  fireEvent.change(screen.getByLabelText('选择需要补充的机会'),{target:{value:'opportunity'}})
  await screen.findByRole('button',{name:'保存这次补充'})
}
afterEach(()=>{cleanup();localStorage.clear();vi.restoreAllMocks()})
it('requires explicit opportunity selection; unknown defaults stay nullable, with automatic evidence beside criteria',async()=>{
  const f=fixture();render(<BehaviorGoalReviewPanel {...props} request={f.request}/>);await open()
  expect(screen.getByLabelText('当时是否有这个机会')).toHaveValue('unknown')
  expect(screen.getByLabelText('谁发起这次行动')).toHaveValue('unknown')
  expect(screen.getByLabelText('协助程度')).toHaveValue('unknown')
  expect(screen.getAllByText(/自动证据：未知/).length).toBe(3)
  fireEvent.click(screen.getByRole('button',{name:'保存这次补充'}))
  await screen.findByText(/成长模型已按当前证据重读/)
  const body=JSON.parse(String(f.posts()[0][1]?.body))
  expect(body.opportunity_present).toBeNull();expect(Object.values(body.criteria)).toEqual([null,null,null])
  expect(body).not.toHaveProperty('actor_type')
  expect(screen.getByText(/当前家长补充/)).toBeInTheDocument()
})
it('saves draft and lost-response exact command across remount; never auto-replays and re-reads current after retry',async()=>{
  const f=fixture(); const mounted=render(<BehaviorGoalReviewPanel {...props} request={f.request}/>);await open()
  fireEvent.change(screen.getByLabelText('补充说明（可选）'),{target:{value:'当时我只提醒了一次'}})
  f.lose();fireEvent.click(screen.getByRole('button',{name:'保存这次补充'}));await screen.findByText(/未收到可核对的回执/)
  const original=f.posts()[0][1]?.body
  mounted.unmount();render(<BehaviorGoalReviewPanel {...props} request={f.request}/>)
  fireEvent.click(screen.getByText('核对具体机会'));await screen.findByRole('button',{name:'按原操作核对回执'})
  expect(f.posts()).toHaveLength(1)
  fireEvent.click(screen.getByRole('button',{name:'按原操作核对回执'}))
  await screen.findByText(/成长模型已按当前证据重读/)
  expect(f.posts()).toHaveLength(2);expect(f.posts()[1][1]?.body).toBe(original)
  expect(JSON.parse(localStorage.getItem(behaviorGoalLocalKey('a','goal'))!).pending).toBeNull()
})
it('changed source drops old pending and yes values but retains the written explanation',async()=>{
  const f=fixture();const r=render(<BehaviorGoalReviewPanel {...props} request={f.request}/>);await open()
  fireEvent.change(screen.getByLabelText('当时是否有这个机会'),{target:{value:'yes'}})
  fireEvent.change(screen.getByLabelText('补充说明（可选）'),{target:{value:'原说明保留供重新核对'}})
  f.lose();fireEvent.click(screen.getByRole('button',{name:'保存这次补充'}));await screen.findByText(/未收到可核对的回执/)
  r.unmount();const changed=goalView();changed.opportunities[0].source_version='f'.repeat(64);changed.opportunities[0].binding_version='f'.repeat(64);f.set(changed)
  render(<BehaviorGoalReviewPanel {...props} request={f.request}/>);fireEvent.click(screen.getByText('核对具体机会'))
  await screen.findByText(/旧操作已停用/)
  expect(screen.getByLabelText('当时是否有这个机会')).toHaveValue('unknown')
  expect(screen.getByLabelText('补充说明（可选）')).toHaveValue('原说明保留供重新核对')
  expect(f.posts()).toHaveLength(1)
})
it('retracts current confirmation with new binding, then restart shows no active confirmation',async()=>{
  const f=fixture();const r=render(<BehaviorGoalReviewPanel {...props} request={f.request}/>);await open()
  fireEvent.click(screen.getByRole('button',{name:'保存这次补充'}));await screen.findByRole('button',{name:'撤回这次补充'})
  fireEvent.click(screen.getByRole('button',{name:'撤回这次补充'}));await screen.findByText(/补充已撤回/)
  expect(JSON.parse(String(f.posts()[1][1]?.body))).toMatchObject({operation:'retract',expected_version:'d'.repeat(64)})
  r.unmount();render(<BehaviorGoalReviewPanel {...props} request={f.request}/>);fireEvent.click(screen.getByText('核对具体机会'))
  await screen.findByText('当前没有有效补充；未知不计为失败。')
  expect(screen.queryByRole('button',{name:'撤回这次补充'})).not.toBeInTheDocument()
})
it('a failed model refresh or parent callback cannot reclassify a persisted decision as a failed save',async()=>{
  const f=fixture();f.modelFail();const onChanged=vi.fn().mockRejectedValue(new Error('refresh'))
  render(<BehaviorGoalReviewPanel {...props} request={f.request} onChanged={onChanged}/>);await open()
  fireEvent.click(screen.getByRole('button',{name:'保存这次补充'}));await screen.findByText(/补充已记录，但部分结果尚未刷新/)
  expect(f.posts()).toHaveLength(1)
  expect(JSON.parse(localStorage.getItem(behaviorGoalLocalKey('a','goal'))!).pending).toBeNull()
  expect(screen.queryByRole('button',{name:'按原操作核对回执'})).not.toBeInTheDocument()
})
it('clear and child switch fence late reads without destroying the other child journal',async()=>{
  let resolve!:(v:unknown)=>void
  const request=vi.fn<BehaviorGoalRequest>(()=>new Promise<unknown>(r=>{resolve=r}) as never)
  const r=render(<BehaviorGoalReviewPanel {...props} request={request}/>);fireEvent.click(screen.getByText('核对具体机会'))
  await waitFor(()=>expect(request).toHaveBeenCalled())
  r.rerender(<BehaviorGoalReviewPanel {...props} childId="b" request={request}/>)
  await act(async()=>{resolve(goalView('a'))})
  expect(screen.queryByLabelText('选择需要补充的机会')).not.toBeInTheDocument()
  localStorage.setItem(behaviorGoalLocalKey('b','goal'),'other')
  act(()=>clearBehaviorGoalLocalState('a'))
  expect(localStorage.getItem(behaviorGoalLocalKey('b','goal'))).toBe('other')
})
it('blocks submission if local storage fails, or source has been withdrawn',async()=>{
  const f=fixture();render(<BehaviorGoalReviewPanel {...props} request={f.request}/>);await open()
  vi.spyOn(Storage.prototype,'setItem').mockImplementation(()=>{throw new Error('quota')})
  fireEvent.click(screen.getByRole('button',{name:'保存这次补充'}));await screen.findByText(/未能保存本地恢复记录/)
  expect(f.posts()).toHaveLength(0)
})
it('withdrawn source has no active confirmation and cannot be supplemented',async()=>{
  const f=fixture();const v=goalView();v.opportunities[0].source_current=false;f.set(v)
  render(<BehaviorGoalReviewPanel {...props} request={f.request}/>);await open()
  expect(screen.getByRole('button',{name:'保存这次补充'})).toBeDisabled()
  expect(screen.getByRole('alert')).toHaveTextContent('原来源已失效')
  expect(f.posts()).toHaveLength(0)
})
it('a corrupt journal requires explicit abandonment; reloading does not silently clear it or POST',async()=>{
  const f=fixture();localStorage.setItem(behaviorGoalLocalKey('a','goal'),'{')
  render(<BehaviorGoalReviewPanel {...props} request={f.request}/>);fireEvent.click(screen.getByText('核对具体机会'))
  await screen.findByRole('button',{name:'放弃损坏记录'})
  expect(localStorage.getItem(behaviorGoalLocalKey('a','goal'))).toBe('{')
  expect(screen.getByLabelText('选择需要补充的机会')).toBeDisabled()
  fireEvent.click(screen.getByRole('button',{name:'放弃损坏记录'}))
  expect(localStorage.getItem(behaviorGoalLocalKey('a','goal'))).toBeNull()
  expect(f.posts()).toHaveLength(0)
})
it('a child-scoped clear during an in-flight write cannot recreate that child pending state',async()=>{
  let done!:(v:unknown)=>void
  const request=vi.fn<BehaviorGoalRequest>(async(path,init)=>init?.method==='POST'?new Promise(r=>{done=r}):goalView())
  render(<BehaviorGoalReviewPanel {...props} request={request}/>);await open()
  fireEvent.click(screen.getByRole('button',{name:'保存这次补充'}));await waitFor(()=>expect(done).toBeDefined())
  act(()=>clearBehaviorGoalLocalState('a'))
  await act(async()=>done({status:'recorded',event_id:'late',replayed:false,historical_receipt:false,local_only:true,cloud_transmission_allowed:false}))
  expect(localStorage.getItem(behaviorGoalLocalKey('a','goal'))).toBeNull()
  expect(screen.queryByText(/当前家长补充/)).not.toBeInTheDocument()
})
it('a 404 after refresh removes the previous opportunity evidence from view',async()=>{
  const f=fixture();render(<BehaviorGoalReviewPanel {...props} request={f.request}/>);await open()
  f.request.mockRejectedValueOnce(new ApiError('missing',404))
  fireEvent.click(screen.getByRole('button',{name:'刷新当前证据'}))
  await screen.findByText('这项实验不存在或不属于当前孩子。')
  expect(screen.queryByLabelText('当时是否有这个机会')).not.toBeInTheDocument()
  expect(f.posts()).toHaveLength(0)
})
it('a draft survives remount without claiming it is a current confirmation',async()=>{
  const f=fixture();const r=render(<BehaviorGoalReviewPanel {...props} request={f.request}/>);await open()
  fireEvent.change(screen.getByLabelText('协助程度'),{target:{value:'prompted'}})
  fireEvent.change(screen.getByLabelText('补充说明（可选）'),{target:{value:'只给过一句提示'}})
  r.unmount();render(<BehaviorGoalReviewPanel {...props} request={f.request}/>);fireEvent.click(screen.getByText('核对具体机会'))
  await screen.findByDisplayValue('只给过一句提示')
  expect(screen.getByLabelText('协助程度')).toHaveValue('prompted')
  expect(screen.getByText('当前没有有效补充；未知不计为失败。')).toBeInTheDocument()
  expect(f.posts()).toHaveLength(0)
})
