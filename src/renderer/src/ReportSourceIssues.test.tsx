import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import * as api from './api'
import { ReportSourceIssues } from './ReportSourceIssues'

const issue = { issue_id: 'issue-one', child_id: 'one', source_id: 'report-one', status: 'open',
  report_type: 'daily', report_date: '2026-09-08', snapshot_id: 'snapshot-one',
  message: '缺少原始反思，旧稿不能批准或发送。', recovery_hint: '核对原始记录，不补造反思。' }
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('reads durable issues and opens the exact retained report; failed open remains visible', async () => {
  const read = vi.spyOn(api, 'requestParentLocal').mockResolvedValue({ child_id: 'one', items: [issue] })
  const open = vi.fn().mockRejectedValue(new Error('unavailable'))
  render(<ReportSourceIssues childId="one" ready refreshVersion={1} onOpen={open} />)
  fireEvent.click(await screen.findByText('查看保留的原报告'))
  expect(open).toHaveBeenCalledWith('snapshot-one')
  expect(await screen.findByText(/原报告暂不能打开/)).toBeInTheDocument()
  expect(screen.getByText(issue.message)).toBeInTheDocument()
  expect(read).toHaveBeenCalledWith('/v1/parent/children/one/report-source-issues', { cache: 'no-store' })
  expect(screen.queryByText('report-one')).not.toBeInTheDocument()
})

it('reload and remount use current persisted issues, never a local completed flag', async () => {
  const read = vi.spyOn(api, 'requestParentLocal').mockResolvedValue({ child_id: 'one', items: [issue] })
  const props = { childId: 'one', ready: true, refreshVersion: 1, onOpen: vi.fn() }
  const view = render(<ReportSourceIssues {...props} />)
  expect(await screen.findByText(issue.message)).toBeInTheDocument()
  read.mockResolvedValue({ child_id: 'one', items: [] })
  await act(async () => { view.rerender(<ReportSourceIssues {...props} refreshVersion={2} />) })
  expect(screen.queryByText(issue.message)).not.toBeInTheDocument()
  view.unmount()
  read.mockResolvedValue({ child_id: 'one', items: [issue] })
  render(<ReportSourceIssues {...props} />)
  expect(await screen.findByText(issue.message)).toBeInTheDocument()
})

it('rejects wrong-child and late responses; unavailable is not zero', async () => {
  let resolve!: (value: unknown) => void
  const read = vi.spyOn(api, 'requestParentLocal').mockImplementationOnce(() => new Promise(done => { resolve = done }))
  const view = render(<ReportSourceIssues childId="one" ready refreshVersion={1} onOpen={vi.fn()} />)
  read.mockResolvedValue({ child_id: 'one', items: [issue] })
  view.rerender(<ReportSourceIssues childId="two" ready refreshVersion={1} onOpen={vi.fn()} />)
  expect(await screen.findByText(/不能据此认为没有问题/)).toBeInTheDocument()
  await act(async () => { resolve({ child_id: 'one', items: [issue] }) })
  expect(screen.queryByText(issue.message)).not.toBeInTheDocument()
})

it('opens a legacy original as explicitly unverified without offering send or reissue', async () => {
  const old={...issue,snapshot_id:null,error_code:'REPORT_TASK_SOURCE_LEGACY_UNVERIFIED',message:'旧版日报缺少任务来源校验信息，历史内容无法核验；原报告和已发记录仍保留。'}
  vi.spyOn(api,'requestParentLocal').mockImplementation(async path => {
    if (path.endsWith('/report-source-issues')) return {child_id:'one',items:[old]} as never
    return {child_id:'one',report_id:'report-one',report_date:'2026-09-08',verification:'legacy_unverified',
      can_send:false,can_create_trusted_report:false,local_only:true,source_digest:'a'.repeat(64),expires_at:new Date(Date.now()+60000).toISOString(),
      summary:{headline:'旧稿原始标题',facts:['旧稿原始事实'],interpretation:'旧稿原解释'},
      parent_coaching:{scenario:'旧稿场景',script:['旧稿建议'],avoid:[]}} as never
  })
  render(<ReportSourceIssues childId="one" ready refreshVersion={1} onOpen={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button',{name:'查看旧日报原稿'}))
  expect(await screen.findByText('旧稿原始标题')).toBeInTheDocument()
  expect(screen.getByText('旧稿原始事实')).toBeInTheDocument()
  expect(screen.getByText(/历史内容未经核验/)).toBeInTheDocument()
  expect(screen.queryByRole('button',{name:/发送|批准|重发|生成新版/})).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'收起原稿'}))
  expect(screen.queryByText('旧稿原始标题')).not.toBeInTheDocument()
})

it('clears a viewed legacy original when parent authority is revoked', async () => {
  const old={...issue,snapshot_id:null,error_code:'REPORT_TASK_SOURCE_LEGACY_UNVERIFIED'}
  vi.spyOn(api,'requestParentLocal').mockImplementation(async path => path.endsWith('/report-source-issues')
    ? {child_id:'one',items:[old]} as never
    : {child_id:'one',report_id:'report-one',report_date:'2026-09-08',verification:'legacy_unverified',
       can_send:false,can_create_trusted_report:false,local_only:true,source_digest:'a'.repeat(64),expires_at:new Date(Date.now()+60000).toISOString(),
       summary:{headline:'仅本机原稿',facts:[]},parent_coaching:{script:[],avoid:[]}} as never)
  render(<ReportSourceIssues childId="one" ready refreshVersion={1} onOpen={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button',{name:'查看旧日报原稿'}))
  expect(await screen.findByText('仅本机原稿')).toBeInTheDocument()
  act(() => { window.dispatchEvent(new Event('hsc:parent-draft-authority-changed')) })
  expect(screen.queryByText('仅本机原稿')).not.toBeInTheDocument()
})

it('rejects a wrong-child legacy response and does not expose its contents', async () => {
  const old={...issue,snapshot_id:null,error_code:'REPORT_TASK_SOURCE_LEGACY_UNVERIFIED'}
  vi.spyOn(api,'requestParentLocal').mockImplementation(async path => path.endsWith('/report-source-issues')
    ? {child_id:'one',items:[old]} as never
    : {child_id:'two',report_id:'report-one',report_date:'2026-09-08',verification:'legacy_unverified',
       can_send:false,can_create_trusted_report:false,local_only:true,source_digest:'a'.repeat(64),expires_at:new Date(Date.now()+60000).toISOString(),
       summary:{headline:'另一孩子的私密内容',facts:[]},parent_coaching:{script:[],avoid:[]}} as never)
  render(<ReportSourceIssues childId="one" ready refreshVersion={1} onOpen={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button',{name:'查看旧日报原稿'}))
  expect(await screen.findByText(/旧日报原稿暂时无法安全读取/)).toBeInTheDocument()
  expect(screen.queryByText('另一孩子的私密内容')).not.toBeInTheDocument()
})

it('does not reveal a late original after parent authority changes', async () => {
  const old={...issue,snapshot_id:null,error_code:'REPORT_TASK_SOURCE_LEGACY_UNVERIFIED'}
  let finish!: (value: unknown)=>void
  vi.spyOn(api,'requestParentLocal').mockImplementation(path => path.endsWith('/report-source-issues')
    ? Promise.resolve({child_id:'one',items:[old]}) as never
    : new Promise(resolve => {finish=resolve}) as never)
  render(<ReportSourceIssues childId="one" ready refreshVersion={1} onOpen={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button',{name:'查看旧日报原稿'}))
  act(() => { window.dispatchEvent(new Event('hsc:parent-draft-authority-changed')) })
  await act(async () => { finish({child_id:'one',report_id:'report-one',report_date:'2026-09-08',verification:'legacy_unverified',
    can_send:false,can_create_trusted_report:false,local_only:true,source_digest:'a'.repeat(64),expires_at:new Date(Date.now()+60000).toISOString(),
    summary:{headline:'已经锁定的原稿',facts:[]},parent_coaching:{script:[],avoid:[]}}) })
  expect(screen.queryByText('已经锁定的原稿')).not.toBeInTheDocument()
})

it('closes an original if the stored source changes while it is displayed', async () => {
  vi.useFakeTimers()
  const old={...issue,snapshot_id:null,error_code:'REPORT_TASK_SOURCE_LEGACY_UNVERIFIED'}
  const expires=new Date(Date.now()+60000).toISOString()
  vi.spyOn(api,'requestParentLocal').mockImplementation(async path => {
    if (path.endsWith('/report-source-issues')) return {child_id:'one',items:[old]} as never
    if (path.endsWith('/original/status')) return {child_id:'one',report_id:'report-one',verification:'legacy_unverified',source_digest:'b'.repeat(64),expires_at:expires} as never
    return {child_id:'one',report_id:'report-one',report_date:'2026-09-08',verification:'legacy_unverified',
      can_send:false,can_create_trusted_report:false,local_only:true,source_digest:'a'.repeat(64),expires_at:expires,
      summary:{headline:'保留原稿',facts:[]},parent_coaching:{script:[],avoid:[]}} as never
  })
  render(<ReportSourceIssues childId="one" ready refreshVersion={1} onOpen={vi.fn()} />)
  await act(async()=>{})
  fireEvent.click(screen.getByRole('button',{name:'查看旧日报原稿'}))
  await act(async()=>{})
  expect(screen.getByText('保留原稿')).toBeInTheDocument()
  await act(async()=>{vi.advanceTimersByTime(5000);await Promise.resolve()})
  expect(screen.queryByText('保留原稿')).not.toBeInTheDocument()
})

it('clears a legacy original when the parent session expires', async () => {
  vi.useFakeTimers()
  const old={...issue,snapshot_id:null,error_code:'REPORT_TASK_SOURCE_LEGACY_UNVERIFIED'}
  const expires=new Date(Date.now()+1000).toISOString()
  vi.spyOn(api,'requestParentLocal').mockImplementation(async path => path.endsWith('/report-source-issues')
    ? {child_id:'one',items:[old]} as never
    : {child_id:'one',report_id:'report-one',report_date:'2026-09-08',verification:'legacy_unverified',
       can_send:false,can_create_trusted_report:false,local_only:true,source_digest:'a'.repeat(64),expires_at:expires,
       summary:{headline:'即将过期的原稿',facts:[]},parent_coaching:{script:[],avoid:[]}} as never)
  render(<ReportSourceIssues childId="one" ready refreshVersion={1} onOpen={vi.fn()} />)
  await act(async()=>{})
  fireEvent.click(screen.getByRole('button',{name:'查看旧日报原稿'}))
  await act(async()=>{})
  expect(screen.getByText('即将过期的原稿')).toBeInTheDocument()
  act(()=>{vi.advanceTimersByTime(1000)})
  expect(screen.queryByText('即将过期的原稿')).not.toBeInTheDocument()
})
