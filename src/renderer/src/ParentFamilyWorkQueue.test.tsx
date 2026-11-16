import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ParentFamilyWorkQueue, type FamilyWorkPage, type FamilyWorkItem } from './ParentFamilyWorkQueue'
import { localApi } from './api'
import { loadOcrReviewRecovery, saveOcrReviewRecovery } from './ocrReviewRecovery'

afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks() })
const children = [{ child_id: 'one', nickname: '甲' }, { child_id: 'two', nickname: '乙' }]
const item = (id='r1', child='one'): FamilyWorkItem => ({ work_id:'ocr:'+id, kind:'ocr', child_id:child, child_name:child==='one'?'甲':'乙',
  object_id:id, version:'1', title:'校对文字', impact:'未核对可能影响教学', priority:30, urgency:'review', created_at:'2026-09-07',
  evidence:[{label:'识别候选',text:'合成题目一'}], target:{panel:'parent-work-ocr', object_id:id, related_id:'artifact-'+id}, bulk_action:'ocr_skip' })
const page = (items=[item()]): FamilyWorkPage => ({ items, children, sources:[{child_id:'one', kind:'ocr', status:'ready', total:43,error_code:null}],
  total:43,known_total:43,revision:'v1',next_cursor:null,read_at:'2026-09-07',local_only:true,cloud_transmission_allowed:false })
const props = (request: ReturnType<typeof vi.fn>) => ({ children, request: request as never, onOpen:vi.fn().mockResolvedValue(undefined), onAuthNeeded:vi.fn() })

it('shows true totals and exact evidence; rejected object navigation stays selected', async () => {
  const p = props(vi.fn().mockResolvedValue(page()))
  p.onOpen.mockRejectedValue(new Error('该对象尚未加载'))
  render(<ParentFamilyWorkQueue {...p}/>)
  expect(await screen.findByText('43 项待处理')).toBeInTheDocument()
  expect(screen.getByText('合成题目一')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'查看并处理'}))
  await screen.findByText('该对象尚未加载')
  expect(p.onOpen).toHaveBeenCalledWith(expect.objectContaining({child_id:'one',object_id:'r1',panel:'parent-work-ocr',related_id:'artifact-r1'}))
  expect(screen.getByRole('article')).toHaveAttribute('aria-current','true')
})

it('requests the server-bounded parent decision audience and reports system verification backlog', async () => {
  const safety={...item('safe'),work_id:'safety:safe',kind:'safety' as const,title:'线下确认孩子安全',bulk_action:null,
    target:{panel:'parent-work-safety',object_id:'safe',related_id:null}}
  const request=vi.fn().mockResolvedValue({...page([safety]),total:1,known_total:1,system_verification_total:12,
    sources:[{child_id:'one',kind:'safety' as const,status:'ready' as const,total:1,error_code:null}]})
  render(<ParentFamilyWorkQueue {...props(request)} audience="parent-decisions"/>)
  await screen.findByText('1 项待处理')
  expect(request).toHaveBeenCalledWith(expect.stringContaining('audience=parent_decisions'))
  expect(screen.getByText(/另有 12 项在系统核对队列中/)).toBeInTheDocument()
  expect(screen.getByText('线下确认孩子安全')).toBeInTheDocument()
})

it('unavailable sources never become zero and do not allow batch selection', async () => {
  const value = {...page(),total:null,sources:[{child_id:'one',kind:'rubric' as const,status:'unavailable' as const,total:null,error_code:'WORK_SOURCE_UNAVAILABLE'}]}
  render(<ParentFamilyWorkQueue {...props(vi.fn().mockResolvedValue(value))}/>)
  expect(await screen.findByText(/已读取 43 项，仍有来源未能核对/)).toBeInTheDocument()
  expect(screen.getByText(/甲 · 开放评分：未能读取/)).toBeInTheDocument()
  expect(screen.getByRole('checkbox')).toBeDisabled()
  expect(screen.queryByText(/没有待办/)).not.toBeInTheDocument()
})

it('ignores late other-child responses and preserves last counts after a read failure', async () => {
  let resolve!: (p:FamilyWorkPage)=>void
  const request = vi.fn().mockImplementationOnce(()=>new Promise<FamilyWorkPage>(r=>{resolve=r})).mockResolvedValue({...page([item('r2','two')]),total:1,known_total:1,sources:[]})
  render(<ParentFamilyWorkQueue {...props(request)}/>)
  fireEvent.change(screen.getByLabelText('待办孩子'),{target:{value:'two'}})
  await screen.findByText('1 项待处理')
  resolve(page())
  await waitFor(()=>expect(screen.queryByText('43 项待处理')).not.toBeInTheDocument())
  request.mockRejectedValue(new Error('断开'))
  fireEvent.click(screen.getByRole('button',{name:'刷新待办'}))
  await screen.findByText(/读取失败，保留上次结果/)
  expect(screen.getByText('1 项待处理')).toBeInTheDocument()
})

it('persists exact OCR batch and artifact mapping before dispatch; remount never retries automatically', async () => {
  const p = props(vi.fn().mockResolvedValue(page()))
  const batch = vi.spyOn(localApi,'batchOcrReview').mockImplementation(async () => {
    const stored = loadOcrReviewRecovery('one')
    expect(stored.artifactIds).toEqual({r1:'artifact-r1'})
    expect(stored.pending).toMatchObject({kind:'batch',reviewIds:['r1'],decision:'skip'})
    throw new Error('response lost')
  })
  const view=render(<ParentFamilyWorkQueue {...p}/>)
  fireEvent.click(await screen.findByRole('checkbox'))
  fireEvent.click(screen.getByRole('button',{name:'跳过所选文字校对'}))
  await screen.findByText(/操作结果待确认/)
  const command=loadOcrReviewRecovery('one').pending
  view.unmount()
  render(<ParentFamilyWorkQueue {...p}/>)
  await screen.findByText(/甲：有校对操作待核对/)
  expect(batch).toHaveBeenCalledTimes(1)
  batch.mockResolvedValue({items:[{review_id:'r1',child_id:'one',status:'skipped'}],processed_count:1,remaining_count:0,decision:'skip'} as never)
  fireEvent.click(screen.getByRole('button',{name:'重试原校对请求'}))
  await waitFor(()=>expect(batch).toHaveBeenCalledTimes(2))
  expect(batch.mock.calls[1]).toEqual(['one',['r1'],'skip',command!.actionId])
  await waitFor(()=>expect(loadOcrReviewRecovery('one').pending).toBeNull())
})

it('does not combine two children into one OCR batch or select unseen pages', async () => {
  render(<ParentFamilyWorkQueue {...props(vi.fn().mockResolvedValue({...page([item(),item('r2','two')]),next_cursor:'opaque'}))}/>)
  await screen.findByText('43 项待处理')
  const checks=screen.getAllByRole('checkbox')
  fireEvent.click(checks[0])
  expect(checks[1]).toBeDisabled()
  expect(screen.queryByRole('checkbox',{name:/全选/})).not.toBeInTheDocument()
})

it('journal changes update pending controls without network reload or overwriting another artifact', async () => {
  const request=vi.fn().mockResolvedValue(page())
  render(<ParentFamilyWorkQueue {...props(request)}/>)
  await screen.findByText('43 项待处理')
  saveOcrReviewRecovery({childId:'one',drafts:{other:{text:'保留人工修正',proposed:'候选'}},artifactIds:{other:'different-page'},pending:{kind:'skip',reviewId:'other',actionId:'pending-other'}})
  await screen.findByText(/甲：有校对操作待核对/)
  expect(screen.getByRole('checkbox')).toBeDisabled()
  expect(request).toHaveBeenCalledTimes(1)
  expect(loadOcrReviewRecovery('one').artifactIds).toEqual({other:'different-page'})
})

it('opens an actual blocked-job detail and never sends its ID to a package panel', async () => {
  const blocked={...item(),work_id:'curriculum:job',kind:'curriculum' as const,object_id:'job',bulk_action:null,
    title:'补齐专项课程供给',target:{panel:'parent-work-supply-gap',object_id:'job',related_id:'seed'}}
  const request=vi.fn().mockResolvedValueOnce(page([blocked])).mockResolvedValueOnce({child_id:'one',job_id:'job',status:'blocked',evidence:[{label:'原题',text:'真实合成专项题'}]})
  const p=props(request)
  render(<ParentFamilyWorkQueue {...p}/>)
  fireEvent.click(await screen.findByRole('button',{name:'查看并处理'}))
  await screen.findByRole('region',{name:'供给阻塞详情'})
  expect(screen.getByText('真实合成专项题')).toBeInTheDocument()
  expect(p.onOpen).not.toHaveBeenCalled()
  expect(request).toHaveBeenLastCalledWith('/v1/parent/children/one/work-supply-gaps/job')
})

it('opens original evidence in its own review panel without misrouting to a mistake or curriculum',async()=>{
  const original={...item(),work_id:'original_question:work',kind:'original_question' as const,object_id:'work',bulk_action:null,
    title:'核对原题学习目标',target:{panel:'parent-work-original-check',object_id:'work',related_id:null}}
  const request=vi.fn().mockImplementation(async(path:string)=>path.includes('/original-checks/')?{
    child_id:'one',commitment_id:'work',version:'v1',status:'mapping_required',original_question:'未标为错题的原题',subject:'数学',references:[],truncated:false,mapping:null,
    local_only:true,cloud_transmission_allowed:false,mastery_update_allowed:false
  }:page([original]))
  const p=props(request);render(<ParentFamilyWorkQueue {...p}/>)
  fireEvent.click(await screen.findByRole('button',{name:'查看并处理'}))
  await screen.findByText('未标为错题的原题');expect(p.onOpen).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText('待办孩子'),{target:{value:'two'}})
  await waitFor(()=>expect(screen.queryByRole('region',{name:'原题学习目标审核'})).not.toBeInTheDocument())
})
