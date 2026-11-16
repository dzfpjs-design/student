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
