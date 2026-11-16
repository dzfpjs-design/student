import { cleanup, fireEvent, render, screen, waitFor, act } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { ReportFeedbackHistory } from './ReportFeedbackHistory'
import type { ReportFeedbackRevision } from './types'

const saved: ReportFeedbackRevision = { event_id: 'private-event-id', revision: 1, operation: 'record', created_at: '2026-09-07T10:00:00Z', payload: { helpfulness: 4, stress_rating: 2, notes: '只试了一个方法' } }
const removed: ReportFeedbackRevision = { event_id: 'private-retract-id', revision: 2, operation: 'retract', created_at: '2026-09-07T11:00:00Z', payload: { reason: '家长撤回本次评价' } }
afterEach(() => { cleanup(); vi.restoreAllMocks() })

function open(): void { fireEvent.click(screen.getByText('已保存的评价与撤回')) }

it('loads readable evidence on demand and refreshes persisted retraction', async () => {
  const history = vi.spyOn(localApi, 'reportFeedbackHistory').mockResolvedValueOnce([saved]).mockResolvedValue([saved, removed])
  const retract = vi.spyOn(localApi, 'retractReportFeedback').mockResolvedValue({ event_id: removed.event_id })
  const changed = vi.fn().mockResolvedValue(undefined)
  render(<ReportFeedbackHistory childId="a" snapshotId="report-a" onChanged={changed} />)
  expect(history).not.toHaveBeenCalled()
  open()
  expect(await screen.findByText('只试了一个方法')).toBeInTheDocument()
  expect(screen.queryByText(saved.event_id)).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '撤回这次评价' }))
  expect(await screen.findByText('这次评价已撤回，历史记录保留。')).toBeInTheDocument()
  expect(retract).toHaveBeenCalledExactlyOnceWith('a', 'report-a', 1)
  expect(changed).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('button', { name: '撤回这次评价' })).not.toBeInTheDocument()
})

it('keeps uncertain retraction retryable with the original revision', async () => {
  vi.spyOn(localApi, 'reportFeedbackHistory').mockResolvedValue([saved])
  const retract = vi.spyOn(localApi, 'retractReportFeedback').mockRejectedValueOnce(new Error('lost response')).mockResolvedValue({ event_id: removed.event_id })
  render(<ReportFeedbackHistory childId="a" snapshotId="report-a" onChanged={vi.fn().mockResolvedValue(undefined)} />)
  open()
  fireEvent.click(await screen.findByRole('button', { name: '撤回这次评价' }))
  expect(await screen.findByText(/尚未确认撤回结果/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '撤回这次评价' }))
  await waitFor(() => expect(retract).toHaveBeenCalledTimes(2))
  expect(retract.mock.calls).toEqual([['a', 'report-a', 1], ['a', 'report-a', 1]])
})

it('discards an old child response after switching scope', async () => {
  let resolve!: (rows: ReportFeedbackRevision[]) => void
  vi.spyOn(localApi, 'reportFeedbackHistory').mockImplementationOnce(() => new Promise(done => { resolve = done })).mockResolvedValue([])
  const view = render(<ReportFeedbackHistory childId="a" snapshotId="report-a" onChanged={vi.fn()} />)
  open()
  await waitFor(() => expect(localApi.reportFeedbackHistory).toHaveBeenCalledTimes(1))
  view.rerender(<ReportFeedbackHistory childId="b" snapshotId="report-b" onChanged={vi.fn()} />)
  await act(async () => resolve([saved]))
  open()
  expect(await screen.findByText('还没有已保存的评价。')).toBeInTheDocument()
  expect(screen.queryByText('只试了一个方法')).not.toBeInTheDocument()
})
