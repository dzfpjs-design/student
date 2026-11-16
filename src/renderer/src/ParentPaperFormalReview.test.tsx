import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import {ParentPaperFormalReview} from './ParentPaperFormalReview'
vi.mock('./CurriculumSupplyReviewPanel',()=>({CurriculumSupplyReviewPanel:({focusRequest}:{focusRequest?:{objectId:string}})=><section aria-label="原有正式审核队列">{focusRequest?.objectId}</section>}))
afterEach(()=>{cleanup();vi.useRealTimers()})
const options={status:'ready',child:{child_id:'public-child',nickname:'公开合成孩子',grade:'四年级'},blockers:[],
  draft_identity:{revision:2,storage_generation:'public-generation',last_action_id:'public-save-1'},
  selection:{version:3,scope:{edition:'公开合成教材',semester:'上学期'}},node:{id:'n',title:'整数加法',subject:'数学',binding:'b'.repeat(64)},
  seeds:[{diagnostic_item_id:'public-root-Q7',prompt:'公开合成已经审核的原题七。',binding:'c'.repeat(64)}],
  standards:[{key:'req:12',title:'运算律',standard_page:12,standard_quote:'公开合成已审核课标完整依据。',binding:'a'.repeat(64)}]}
function service(){let lose=true;const requests:{path:string;body:Record<string,unknown>}[]=[]
  const api=vi.fn(async(path:string,init?:RequestInit):Promise<unknown>=>{
    if(path.endsWith('/submissions'))return {items:[]}
    if(path.endsWith('/options'))return options
    if(path.endsWith('/drafts/capture'))return {draft:{storage_generation:'public-generation'}}
    const body=JSON.parse(String(init?.body));requests.push({path,body})
    if(path.endsWith('/generate'))return {receipt:{receipt_id:'public-receipt',route:'parallel'},candidate:{candidate_id:'public-formal-candidate',child_id:'public-child'},child:options.child}
    if(lose){lose=false;throw Error('public lost reply, outcome unknown')}
    return {receipt_id:'public-receipt',route:'parallel',diagnostic_item_id:null}
  });return {api,requests}}
async function choose(){
  fireEvent.click(screen.getByRole('button',{name:'核对已保存稿的送审条件'}))
  await screen.findByLabelText('答案如何评分')
  fireEvent.change(screen.getByLabelText('答案如何评分'),{target:{value:'ordered_numeric_parts'}})
  fireEvent.change(screen.getByLabelText('对应的已审核原题'),{target:{value:'public-root-Q7'}})
  fireEvent.change(screen.getByLabelText('对应课标'),{target:{value:'req:12'}})
  fireEvent.change(screen.getByLabelText('原页使用范围'),{target:{value:'public'}})
  fireEvent.click(screen.getByRole('checkbox'))
}
it('requires explicit meaning and original confirmation; unknown reply retries the exact action then only generates an unapproved candidate',async()=>{
  const s=service(),save=vi.fn(async()=>undefined)
  render(<ParentPaperFormalReview childId="public-child" api={s.api} onSave={save} blocked={false} draftBinding="public-Q6"/>)
  await choose();expect(save).toHaveBeenCalledOnce()
  fireEvent.click(screen.getByRole('button',{name:'送审核（不批准、不发布）'}))
  await screen.findByText(/本次送审结果尚未确认/)
  const before=s.requests[0].body
  expect(before.seed_choice).toEqual({diagnostic_item_id:'public-root-Q7',binding:'c'.repeat(64)})
  expect(before.answer_semantics).toEqual({kind:'ordered_numeric_parts',numeric_kind:'integer',tuple_size:2})
  expect(screen.getByLabelText('答案如何评分')).toBeDisabled()
  fireEvent.click(screen.getByRole('button',{name:'核对上次送审'}))
  await screen.findByText('public-formal-candidate')
  expect(s.requests[1].body).toEqual(before)
  expect(s.requests.map(r=>r.path).some(p=>/\/(review|publish)$/.test(p))).toBe(false)
})
it('a child switch removes old source and pending UI synchronously; a late source reply cannot generate under the other child',async()=>{
  let resolve!:(v:unknown)=>void
  const api=vi.fn(async(path:string,init?:RequestInit)=>path.endsWith('/submissions')?{items:[]}:path.endsWith('/options')?options:new Promise(r=>{resolve=r}))
  const view=render(<ParentPaperFormalReview childId="public-child" api={api} onSave={async()=>undefined} blocked={false} draftBinding="public-Q6"/>)
  await choose();fireEvent.click(screen.getByRole('button',{name:'送审核（不批准、不发布）'}))
  await waitFor(()=>expect(resolve).toBeTypeOf('function'))
  view.rerender(<ParentPaperFormalReview childId="public-other-child" api={api} onSave={async()=>undefined} blocked={false} draftBinding="public-Q6"/>)
  expect(screen.queryByText(/公开合成教材/)).toBeNull()
  resolve({receipt_id:'public-old-receipt',route:'parallel'})
  await Promise.resolve();await Promise.resolve()
  expect(api.mock.calls.some(([p])=>p.endsWith('/generate'))).toBe(false)
})
it('editing while prepare is in flight cannot reinstate old submission choices',async()=>{
  let answer!:(v:unknown)=>void
  const api=vi.fn(async(path:string)=>path.endsWith('/submissions')?{items:[]}:new Promise(resolve=>{answer=resolve}))
  const view=render(<ParentPaperFormalReview childId="public-child" api={api} onSave={async()=>undefined} blocked={false} draftBinding="Q6"/>)
  fireEvent.click(screen.getByRole('button',{name:'核对已保存稿的送审条件'}))
  await waitFor(()=>expect(answer).toBeTypeOf('function'))
  view.rerender(<ParentPaperFormalReview childId="public-child" api={api} onSave={async()=>undefined} blocked={false} draftBinding="Q7"/>)
  answer(options);await waitFor(()=>expect(screen.getByRole('button',{name:'核对已保存稿的送审条件'})).not.toBeDisabled())
  expect(screen.queryByLabelText('答案如何评分')).toBeNull()
  expect(api.mock.calls.some(([p])=>p.endsWith('/generate'))).toBe(false)
})

it('a never-settling saved-record GET has a bounded wait and retry; late old response cannot replace the retry',async()=>{
  let reads=0,late!:(v:unknown)=>void,signal:AbortSignal|undefined
  const api=vi.fn(async(path:string,init?:RequestInit)=>{
    if(!path.endsWith('/submissions'))throw Error('unexpected public request')
    reads++
    if(reads===2){signal=init?.signal??undefined;return new Promise(resolve=>{late=resolve})}
    return {items:reads===1?[]:[{audit_id:'new-record',occurred_at:'2026-10-17',title:'单题原页核对 · 7',state:'published'}]}
  })
  render(<ParentPaperFormalReview childId="public-child" api={api} onSave={async()=>undefined} blocked={false} draftBinding="Q6"/>)
  await act(async()=>{})
  vi.useFakeTimers()
  fireEvent.click(screen.getByRole('button',{name:'刷新已保存送审'}))
  await act(async()=>{await vi.advanceTimersByTimeAsync(10001)})
  expect(screen.getByRole('button',{name:'刷新已保存送审'})).not.toBeDisabled()
  expect(signal?.aborted).toBe(true)
  expect(screen.getByRole('status')).toHaveTextContent(/尚未读到/)
  fireEvent.click(screen.getByRole('button',{name:'刷新已保存送审'}));await act(async()=>{})
  expect(screen.getByRole('button',{name:/继续送审：单题原页核对 · 7/})).toBeInTheDocument()
  await act(async()=>{late({items:[{audit_id:'old-record',occurred_at:'old',title:'错误旧记录',state:'published'}]})})
  expect(screen.queryByText(/错误旧记录/)).toBeNull()
})

it('an unacknowledged POST times out without replacing its action; retry uses the exact body and late old success cannot generate',async()=>{
  let posts=0,late!:(v:unknown)=>void
  const bodies:unknown[]=[],signals:AbortSignal[]=[]
  const api=vi.fn(async(path:string,init?:RequestInit):Promise<unknown>=>{
    if(path.endsWith('/submissions'))return {items:[]}
    if(path.endsWith('/options'))return options
    if(path.endsWith('/generate'))return {receipt:{route:'parallel'},candidate:{candidate_id:'new-candidate',child_id:'public-child'},child:options.child}
    posts++;bodies.push(JSON.parse(String(init?.body)));signals.push(init?.signal as AbortSignal)
    if(posts===1)return new Promise(resolve=>{late=resolve})
    return {receipt_id:'same-action-server-receipt',route:'parallel',diagnostic_item_id:null}
  })
  render(<ParentPaperFormalReview childId="public-child" api={api} onSave={async()=>undefined} blocked={false} draftBinding="Q6"/>)
  await choose();vi.useFakeTimers()
  fireEvent.click(screen.getByRole('button',{name:'送审核（不批准、不发布）'}))
  await act(async()=>{await vi.advanceTimersByTimeAsync(10001)})
  expect(signals[0].aborted).toBe(true)
  expect(screen.getByRole('button',{name:'核对上次送审'})).not.toBeDisabled()
  expect(screen.getByRole('status')).toHaveTextContent(/结果尚未确认/)
  const original=structuredClone(bodies[0])
  await act(async()=>{late({receipt_id:'late-original',route:'parallel'})})
  expect(api.mock.calls.some(([path])=>path.endsWith('/generate'))).toBe(false)
  fireEvent.click(screen.getByRole('button',{name:'核对上次送审'}));await act(async()=>{})
  expect(bodies).toEqual([original,original])
  expect(screen.getByText('new-candidate')).toBeInTheDocument()
})
it('explicit cancellation and a draft/authority change abort the owned wait and ignore late results without touching another child',async()=>{
  let late!:(v:unknown)=>void,signal:AbortSignal|undefined,reads=0
  const api=vi.fn(async(_path:string,init?:RequestInit):Promise<unknown>=>{
    reads++;if(reads===1)return {items:[]}
    signal=init?.signal??undefined;return new Promise(resolve=>{late=resolve})
  })
  const view=render(<ParentPaperFormalReview childId="public-child" api={api} onSave={async()=>undefined} blocked={false} draftBinding="Q6"/>)
  await act(async()=>{})
  fireEvent.click(screen.getByRole('button',{name:'刷新已保存送审'}))
  expect(screen.getByRole('status')).toHaveTextContent(/正在读取/)
  fireEvent.click(screen.getByRole('button',{name:'停止等待，保留原请求'}))
  expect(signal?.aborted).toBe(true)
  expect(screen.getByRole('button',{name:'刷新已保存送审'})).not.toBeDisabled()
  await act(async()=>{late({items:[{audit_id:'late-cancelled',occurred_at:'now',title:'取消后的旧记录',state:'published'}]})})
  expect(screen.queryByText(/取消后的旧记录/)).toBeNull()
  fireEvent.click(screen.getByRole('button',{name:'刷新已保存送审'}))
  view.rerender(<ParentPaperFormalReview childId="public-child" api={api} onSave={async()=>undefined} blocked={true} draftBinding="Q7"/>)
  expect(signal?.aborted).toBe(true)
  await act(async()=>{late({items:[{audit_id:'late-lock',occurred_at:'now',title:'权限失效后的旧记录',state:'published'}]})})
  expect(screen.queryByText(/权限失效后的旧记录/)).toBeNull()
  view.rerender(<ParentPaperFormalReview childId="public-other" api={api} onSave={async()=>undefined} blocked={false} draftBinding="other"/>)
  expect(screen.queryByRole('button',{name:/继续送审/})).toBeNull()
})
it('a nonsettling draft-save wait cannot trigger options or new publication after timeout',async()=>{
  let saved!:()=>void
  const api=vi.fn(async()=>({items:[]})),onSave=vi.fn(()=>new Promise<void>(resolve=>{saved=resolve}))
  render(<ParentPaperFormalReview childId="public-child" api={api} onSave={onSave} blocked={false} draftBinding="Q6"/>)
  await act(async()=>{});vi.useFakeTimers()
  fireEvent.click(screen.getByRole('button',{name:'核对已保存稿的送审条件'}))
  await act(async()=>{await vi.advanceTimersByTimeAsync(10001)})
  expect(screen.getByRole('button',{name:'核对已保存稿的送审条件'})).not.toBeDisabled()
  expect(screen.getByRole('status')).toHaveTextContent(/尚未确认草稿/)
  await act(async()=>{saved()})
  expect(api.mock.calls).toHaveLength(1)
  expect(screen.queryByLabelText('答案如何评分')).toBeNull()
})

it('resuming a saved receipt has a bounded authority read and never calls follow-up APIs after its late response',async()=>{
  let late!:(v:unknown)=>void
  const api=vi.fn(async(path:string):Promise<unknown>=>path.endsWith('/submissions')?{items:[{
    audit_id:'saved-Q6',occurred_at:'2026-10-17',title:'单题原页核对 · 6',state:'published'}]}:new Promise(resolve=>{late=resolve}))
  render(<ParentPaperFormalReview childId="public-child" api={api} onSave={async()=>undefined} blocked={false} draftBinding="Q6"/>)
  await screen.findByRole('button',{name:/继续送审：单题原页核对 · 6/});vi.useFakeTimers()
  fireEvent.click(screen.getByRole('button',{name:/继续送审：单题原页核对 · 6/}))
  await act(async()=>{await vi.advanceTimersByTimeAsync(10001)})
  expect(screen.getByRole('button',{name:/继续送审：单题原页核对 · 6/})).not.toBeDisabled()
  expect(screen.getByRole('status')).toHaveTextContent(/尚未核对/)
  await act(async()=>{late({draft:{storage_generation:'public-generation'}})})
  expect(api).toHaveBeenCalledTimes(2)
  expect(api.mock.calls.some(([path])=>path.endsWith('/generate'))).toBe(false)
})

it.each([
  ['draft','parallel','待独立校验'],['blocked','parallel','来源或材料待核对'],
  ['validated','parallel','已校验，待人工审核'],['reviewed','parallel','已人工审核，待明确发布'],
  ['published','parallel','已有发布记录'],['withdrawn','parallel','已撤回'],['rejected','parallel','已退回'],
  ['unknown','parallel','当前审核状态暂不能确认'],['published','reuse','沿用已审核的合格原题版本']
])('restores the saved %s/%s status without implying a new approval or publication',async(state,route,label)=>{
  const posts:{path:string;body:unknown}[]=[]
  const api=vi.fn(async(path:string,init?:RequestInit):Promise<unknown>=>{
    if(path.endsWith('/submissions'))return {items:[{audit_id:'public-existing',occurred_at:'2026-10-17T01:00:00Z',title:'公开合成Q6',state}]}
    if(path.endsWith('/drafts/capture'))return {draft:{storage_generation:'public-generation'}}
    if(path.endsWith('/generate')){posts.push({path,body:JSON.parse(String(init?.body))});return {
      receipt:{receipt_id:'public-existing',route},candidate:route==='reuse'?null:{candidate_id:'public-existing-Q6',child_id:'public-child',state,version:4,eligible:state==='published'},child:options.child}}
    return {receipt_id:'public-existing',route,diagnostic_item_id:route==='reuse'?'public-original':null}
  })
  render(<ParentPaperFormalReview childId="public-child" api={api} onSave={async()=>undefined} blocked={false} draftBinding="Q6"/>)
  fireEvent.click(await screen.findByRole('button',{name:/继续送审：公开合成Q6/}))
  await waitFor(()=>expect(screen.getByRole('status')).toHaveTextContent(label))
  expect(screen.getByRole('status')).not.toHaveTextContent('尚未批准或发布')
  fireEvent.click(screen.getByRole('button',{name:/继续送审：公开合成Q6/}))
  await waitFor(()=>expect(posts).toHaveLength(2))
  await waitFor(()=>expect(screen.getByRole('status')).toHaveTextContent(label))
  expect(posts[0].body).toEqual(posts[1].body)
  expect(api.mock.calls.filter(([,init])=>init?.method==='POST').every(([path])=>path.endsWith('/public-existing/generate'))).toBe(true)
})

it('does not mount an unrelated original review panel for an exact existing-original reuse receipt',async()=>{
  const api=vi.fn(async(path:string):Promise<unknown>=>{
    if(path.endsWith('/submissions'))return {items:[{audit_id:'public-reuse',occurred_at:'2026-10-17T01:00:00Z',title:'公开合成Q6',state:'reused'}]}
    if(path.endsWith('/drafts/capture'))return {draft:{storage_generation:'public-generation'}}
    if(path.endsWith('/generate'))return {receipt:{receipt_id:'public-reuse',route:'reuse'},candidate:null,child:options.child}
    return {receipt_id:'public-reuse',route:'reuse',diagnostic_item_id:'public-qualified-original-Q6'}
  })
  render(<ParentPaperFormalReview childId="public-child" api={api} onSave={async()=>undefined} blocked={false} draftBinding="Q6"/>)
  fireEvent.click(await screen.findByRole('button',{name:/继续送审：公开合成Q6/}))
  await waitFor(()=>expect(api.mock.calls.some(([path])=>path.endsWith('/generate'))).toBe(true))
  await act(async()=>{})
  expect(screen.queryByLabelText('原有正式审核队列')).toBeNull()
})
