import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { ParentSupplyFollowupPanel, type SupplyFollowup } from './ParentSupplyFollowupPanel'

afterEach(()=>{cleanup();vi.restoreAllMocks()})
const value=(child='one',root='root1'):SupplyFollowup=>({child_id:child,root_obligation_id:root,
  original_session_id:'yesterday',original_date:'2026-09-09',title:'原写作练习',original_reason:'昨天缺合格原题，未开始',
  decision_id:'today-decision',current_session_id:'today',plan_item_id:null,status:'ready',deferral_reason:'原事项跨日保留',
  next_action:'题目已备好，孩子愿意时说继续原练习。',material_object_id:'material1',version:'v1',local_only:true,cloud_transmission_allowed:false,mastery_claim_allowed:false})
const props=(request=vi.fn().mockResolvedValue(value()))=>({childId:'one',request:request as never,onAuthNeeded:vi.fn(),
  focusRequest:{objectId:'root1',nonce:'open1'},onFocusResolved:vi.fn()})

it('keeps the missing oral question beside the next action and opens its exact source',async()=>{
  const row={...value(),status:'missing_source',subject:'数学',original_question:'解释为什么不能把找回的钱算入支出。',
    next_action:'补入不同题目并校验、审核发布；原回答不重做。',material_object_id:'original-candidate'}
  const open=vi.fn().mockResolvedValue(undefined),read=vi.fn().mockResolvedValue(row)
  const p=props(read)
  const view=render(<ParentSupplyFollowupPanel {...p} onOpenMaterial={open}/>);
  await screen.findByText(row.original_question)
  expect(screen.getByText(row.next_action)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'核对材料'}))
  await waitFor(()=>expect(open).toHaveBeenCalledWith('original-candidate'))
  view.unmount();render(<ParentSupplyFollowupPanel {...p} onOpenMaterial={open}/>);
  await screen.findByText('数学 · 原题记录')
  expect(read).toHaveBeenCalledTimes(2)
})

it('renders exact original reason beside today action and only reads; remount recovers server state',async()=>{
  const p=props(),view=render(<ParentSupplyFollowupPanel {...p}/>);
  await screen.findByText('昨天缺合格原题，未开始')
  expect(screen.getByText('题目已备好，孩子愿意时说继续原练习。')).toBeInTheDocument()
  expect(p.onFocusResolved).toHaveBeenCalledWith('open1')
  expect(screen.getAllByRole('button')).toHaveLength(1)
  expect(p.request).toHaveBeenCalledWith('/v1/parent/children/one/supply-followups/root1',{cache:'no-store'})
  view.unmount();render(<ParentSupplyFollowupPanel {...p}/>);await screen.findByText('原写作练习')
  expect(p.request).toHaveBeenCalledTimes(2)
})

it('failed refresh never offers old ready action as current; explicit retry sees withdrawal',async()=>{
  const request=vi.fn().mockResolvedValueOnce(value()).mockRejectedValueOnce(Error('offline'))
    .mockResolvedValue({...value(),status:'source_changed',version:'v2',next_action:'先核对已撤回材料。'})
  render(<ParentSupplyFollowupPanel {...props(request)}/>);await screen.findByText('原写作练习')
  fireEvent.click(screen.getByRole('button',{name:'刷新原练习状态'}));await screen.findByRole('alert')
  expect(screen.queryByText(value().next_action)).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'刷新原练习状态'}));await screen.findByText('先核对已撤回材料。')
})

it('switching children ignores a late successful old-root response',async()=>{
  let resolve!:(v:SupplyFollowup)=>void
  const request=vi.fn().mockImplementationOnce(()=>new Promise<SupplyFollowup>(r=>{resolve=r})).mockResolvedValue({...value('two','root2'),title:'另一孩原事项'})
  const p=props(request),view=render(<ParentSupplyFollowupPanel {...p}/>);
  view.rerender(<ParentSupplyFollowupPanel {...p} childId="two" focusRequest={{objectId:'root2',nonce:'open2'}}/>);
  await screen.findByText('另一孩原事项');await act(async()=>resolve(value()))
  expect(screen.queryByText('原写作练习')).not.toBeInTheDocument()
  expect(p.onFocusResolved).not.toHaveBeenCalledWith('open1')
})

it.each([401,404])('status %s clears old evidence without a write or retry loop',async status=>{
  const request=vi.fn().mockResolvedValueOnce(value()).mockRejectedValue(new ApiError('拒绝',status)),p=props(request)
  render(<ParentSupplyFollowupPanel {...p}/>);await screen.findByText('原写作练习')
  fireEvent.click(screen.getByRole('button',{name:'刷新原练习状态'}));await screen.findByRole('alert')
  await waitFor(()=>expect(screen.queryByText('原写作练习')).not.toBeInTheDocument())
  expect(request).toHaveBeenCalledTimes(2)
  if(status===401)expect(p.onAuthNeeded).toHaveBeenCalledOnce()
})

it('opens exact material evidence and keeps navigation rejection visible',async()=>{
  const open=vi.fn().mockRejectedValue(Error('missing')),p=props()
  render(<ParentSupplyFollowupPanel {...p} onOpenMaterial={open}/>);await screen.findByText('原写作练习')
  fireEvent.click(screen.getByRole('button',{name:'核对材料'}))
  await screen.findByText('材料未能准确打开，原事项仍保留，请重试。')
  expect(open).toHaveBeenCalledWith('material1')
})
