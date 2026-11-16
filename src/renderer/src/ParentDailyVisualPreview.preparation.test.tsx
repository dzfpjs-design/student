import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import * as api from './api'
import { ParentDailyVisualPreview } from './ParentDailyVisualPreview'

afterEach(() => { cleanup(); vi.restoreAllMocks() })
const payload = (day:string) => ({child_id:'a', learning_date:day, local_only:true, is_formal_report:false, document_observation:{pages:[]}, page_observation_time:{status:'no_segments',pages:[]}})
const status = (day:string, value:string, source_id?:string) => ({child_id:'a',day,delivery_enabled:false,recipients:[{recipient_id:'r',label:'家长',address:'parent@example.test',status:value,source_id}]})
const session = () => ({expires_at:expiry})
let expiry = ''
function setup(kind='not_prepared') {
  expiry = new Date(Date.now()+60000).toISOString()
  const mock = vi.spyOn(api,'requestParentLocal').mockImplementation(async (path, init) => {
    if(path.endsWith('/session')) return session() as never
    if(path.endsWith('/delivery-preparation')) {
      const day=path.split('/').at(-2)!
      if(init?.method==='POST') {kind='prepared'; return {status:'prepared_local_only'} as never}
      return status(day,kind,kind==='source_changed'?'source-1':undefined) as never
    }
    return payload(path.split('/').pop()!) as never
  })
  render(<ParentDailyVisualPreview childId="a" />)
  return mock
}
async function open():Promise<void> {
  fireEvent.click(screen.getByRole('button',{name:'查看记录'}))
  await screen.findByRole('button',{name:'查看日报准备状态'})
  fireEvent.click(screen.getByRole('button',{name:'查看日报准备状态'}))
}

it('prepares for the selected recipient and explicitly remains unsent',async()=>{
  const mock=setup()
  await open()
  fireEvent.click(await screen.findByRole('button',{name:'准备本机草稿'}))
  expect(await screen.findByText(/已准备，尚未发送/)).toBeTruthy()
  const post=mock.mock.calls.find(([,init])=>init?.method==='POST')!
  expect(JSON.parse(String(post[1]?.body))).toEqual({recipient_id:'r'})
  expect(screen.getByText('这里只准备本机草稿，尚未发送邮件。')).toBeTruthy()
})

it('only offers update using the exact prior source and blocks uncertain sends',async()=>{
  const mock=setup('source_changed')
  await open()
  fireEvent.click(await screen.findByRole('button',{name:'更新本机草稿'}))
  await screen.findByText(/已准备，尚未发送/)
  const post=mock.mock.calls.find(([,init])=>init?.method==='POST')!
  expect(JSON.parse(String(post[1]?.body))).toEqual({recipient_id:'r',expected_source_id:'source-1'})
})

it.each(['unknown','needs_authorization','already_sent','blocked'])('shows %s without a prepare action',async(kind)=>{
  setup(kind); await open()
  await screen.findByText(/家长（parent@example.test）/)
  expect(screen.queryByRole('button',{name:'准备本机草稿'})).toBeNull()
  expect(screen.queryByRole('button',{name:'更新本机草稿'})).toBeNull()
})

it('drops late status response after privacy pause',async()=>{
  const mock=setup()
  let resolve!: (value:unknown)=>void
  mock.mockImplementation(async (path) => {
    if(path.endsWith('/session')) return session() as never
    if(path.endsWith('/delivery-preparation')) return new Promise(done=>{resolve=done}) as never
    return payload(path.split('/').pop()!) as never
  })
  await open()
  await waitFor(()=>expect(resolve).toBeTypeOf('function'))
  act(()=>window.dispatchEvent(new Event('hsc-privacy-pause')))
  await act(async()=>resolve(status('2026-01-01','prepared')))
  expect(screen.queryByText(/已准备，尚未发送/)).toBeNull()
  expect(screen.queryByRole('button',{name:'查看日报准备状态'})).toBeNull()
})

it.each(['lock','child','day'])('ignores a late POST after %s',async mode=>{
  expiry = new Date(Date.now()+60000).toISOString()
  let finish!: (value:unknown)=>void
  const mock=vi.spyOn(api,'requestParentLocal').mockImplementation(async (path,init)=>{
    if(path.endsWith('/session')) return session() as never
    if(path.endsWith('/delivery-preparation')) {
      if(init?.method==='POST') return new Promise(done=>{finish=done}) as never
      return status(path.split('/').at(-2)!,'not_prepared') as never
    }
    return payload(path.split('/').pop()!) as never
  })
  const view=render(<ParentDailyVisualPreview childId="a" />)
  await open()
  fireEvent.click(await screen.findByRole('button',{name:'准备本机草稿'}))
  await waitFor(()=>expect(finish).toBeTypeOf('function'))
  const priorGets=mock.mock.calls.filter(([path,init])=>path.endsWith('/delivery-preparation') && !init?.method).length
  if(mode==='lock') act(()=>window.dispatchEvent(new Event('hsc:parent-draft-authority-changed')))
  if(mode==='child') view.rerender(<ParentDailyVisualPreview childId="b" />)
  if(mode==='day') fireEvent.change(screen.getByLabelText('学习记录日期'),{target:{value:'2026-01-01'}})
  await act(async()=>finish({status:'prepared_local_only'}))
  expect(mock.mock.calls.filter(([path,init])=>path.endsWith('/delivery-preparation') && !init?.method)).toHaveLength(priorGets)
  expect(screen.queryByText(/已准备，尚未发送/)).toBeNull()
})

it('displays empty and error states without retrying a POST',async()=>{
  const mock=setup()
  mock.mockImplementation(async(path)=>{
    if(path.endsWith('/session')) return session() as never
    if(path.endsWith('/delivery-preparation')) return {child_id:'a',day:path.split('/').at(-2)!,delivery_enabled:false,recipients:[]} as never
    return payload(path.split('/').pop()!) as never
  })
  await open()
  expect(await screen.findByText('还没有设置日报收件人。')).toBeTruthy()
  mock.mockImplementationOnce(async()=>session() as never).mockRejectedValueOnce(new Error('offline'))
  fireEvent.click(screen.getByRole('button',{name:'查看日报准备状态'}))
  expect(await screen.findByText(/无法读取日报准备状态/)).toBeTruthy()
})

it('clears child observations if a preparation request loses parent authority',async()=>{
  const mock=setup()
  mock.mockImplementation(async(path)=>{
    if(path.endsWith('/session')) return session() as never
    if(path.endsWith('/delivery-preparation')) throw new api.ApiError('locked',401)
    return payload(path.split('/').pop()!) as never
  })
  await open()
  await waitFor(()=>expect(screen.queryByRole('button',{name:'查看日报准备状态'})).toBeNull())
})

it('does not revive a pending preparation after the original session expires',async()=>{
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-11-04T04:00:00Z'))
  expiry = new Date(Date.now()+1000).toISOString()
  let finish!: (value:unknown)=>void
  vi.spyOn(api,'requestParentLocal').mockImplementation(async(path,init)=>{
    if(path.endsWith('/session')) return session() as never
    if(path.endsWith('/delivery-preparation')) {
      if(init?.method==='POST') return new Promise(done=>{finish=done}) as never
      return status(path.split('/').at(-2)!,'not_prepared') as never
    }
    return payload(path.split('/').pop()!) as never
  })
  render(<ParentDailyVisualPreview childId="a" />)
  await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'查看记录'})); await Promise.resolve(); await Promise.resolve()})
  await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'查看日报准备状态'})); await Promise.resolve(); await Promise.resolve(); await Promise.resolve()})
  await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'准备本机草稿'})); await Promise.resolve(); await Promise.resolve()})
  expect(finish).toBeTypeOf('function')
  await act(async()=>{await vi.advanceTimersByTimeAsync(1000)})
  await act(async()=>finish({status:'prepared_local_only'}))
  expect(screen.queryByText(/已准备，尚未发送/)).toBeNull()
  expect(screen.queryByRole('button',{name:'查看日报准备状态'})).toBeNull()
  vi.useRealTimers()
})
