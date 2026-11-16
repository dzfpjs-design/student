import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ApiError, localApi } from './api'
import { ReportPlanAdjustments } from './ReportPlanAdjustments'
import type { ReportPlanAdjustment } from './reportPlanAdjustment'

const saved: ReportPlanAdjustment = { adjustment_id: 'private-adjustment', revision: 1, operation: 'propose', expected_feedback_revision: 3,
  constraint_type: 'daily_total_minutes_cap', value_minutes: 20, start_date: '2026-09-07', end_date: '2099-01-01', client_action_id: 'private-command', source_current: true, learning_effect_verified: false }
afterEach(() => { cleanup(); vi.restoreAllMocks() })
function open(): void { fireEvent.click(screen.getByText('后续计划的时长调整')) }

it('saves a proposal without approving and reads approved or withdrawn receipts', async () => {
  vi.spyOn(localApi, 'reportPlanAdjustments').mockResolvedValue([])
  vi.spyOn(localApi, 'proposeReportPlanAdjustment').mockResolvedValue(saved)
  const review = vi.spyOn(localApi, 'reviewReportPlanAdjustment')
    .mockResolvedValueOnce({ ...saved, operation: 'approve', revision: 2 })
    .mockResolvedValueOnce({ ...saved, operation: 'retract', revision: 3 })
  render(<ReportPlanAdjustments childId="a" snapshotId="report" feedbackRevision={3} />)
  open()
  await screen.findByText('还没有计划调整。')
  fireEvent.click(screen.getByRole('button', { name: '保存待确认调整' }))
  expect(await screen.findByText('调整已保存，等待家长确认。')).toBeInTheDocument()
  expect(review).not.toHaveBeenCalled()
  expect(screen.queryByText('private-adjustment')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '确认此上限' }))
  fireEvent.click(await screen.findByRole('button', { name: '撤回此上限' }))
  await screen.findByText('已撤回。后续排程不再采用此上限，原记录保留。')
  expect(review.mock.calls).toEqual([['a', saved.adjustment_id, 1, 'approve'], ['a', saved.adjustment_id, 2, 'retract']])
})

it('retries an uncertain proposal using the frozen key and values', async () => {
  vi.spyOn(localApi, 'reportPlanAdjustments').mockResolvedValue([])
  const write = vi.spyOn(localApi, 'proposeReportPlanAdjustment').mockRejectedValueOnce(new Error('lost')).mockResolvedValue(saved)
  render(<ReportPlanAdjustments childId="a" snapshotId="report" feedbackRevision={3} />)
  open(); await screen.findByText('还没有计划调整。')
  fireEvent.click(screen.getByRole('button', { name: '保存待确认调整' }))
  await screen.findByText(/尚未确认保存结果/)
  fireEvent.click(screen.getByRole('button', { name: '保存待确认调整' }))
  await waitFor(() => expect(write).toHaveBeenCalledTimes(2))
  expect(write.mock.calls[0]).toEqual(write.mock.calls[1])
  expect(write.mock.calls[0][2].expected_feedback_revision).toBe(3)
})

it('has no new-proposal route for withdrawn feedback and blocks stale approval', async () => {
  vi.spyOn(localApi, 'reportPlanAdjustments').mockResolvedValue([{ ...saved, source_current: false }])
  const review = vi.spyOn(localApi, 'reviewReportPlanAdjustment')
  render(<ReportPlanAdjustments childId="a" snapshotId="report" feedbackRevision={null} />)
  open()
  expect(await screen.findByText(/原评价已失效/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '保存待确认调整' })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '确认此上限' })).toBeDisabled()
  expect(review).not.toHaveBeenCalled()
})

it('retains a conflict as a source correction instead of successful adoption', async () => {
  vi.spyOn(localApi, 'reportPlanAdjustments').mockResolvedValue([saved])
  vi.spyOn(localApi, 'reviewReportPlanAdjustment').mockRejectedValue(new ApiError('changed', 409))
  render(<ReportPlanAdjustments childId="a" snapshotId="report" feedbackRevision={3} />)
  open(); fireEvent.click(await screen.findByRole('button', { name: '确认此上限' }))
  await screen.findByText(/报告或评价已变化/)
  expect(screen.queryByRole('button', { name: '撤回此上限' })).not.toBeInTheDocument()
})

it('ignores a previous child pending read', async () => {
  let finish!: (rows: ReportPlanAdjustment[]) => void
  vi.spyOn(localApi, 'reportPlanAdjustments').mockImplementationOnce(() => new Promise(done => { finish = done })).mockResolvedValue([])
  const view = render(<ReportPlanAdjustments childId="a" snapshotId="report-a" feedbackRevision={3} />)
  open(); await waitFor(() => expect(localApi.reportPlanAdjustments).toHaveBeenCalledTimes(1))
  view.rerender(<ReportPlanAdjustments childId="b" snapshotId="report-b" feedbackRevision={1} />)
  await act(async () => finish([saved]))
  open(); await screen.findByText('还没有计划调整。')
  expect(screen.queryByRole('button', { name: '确认此上限' })).not.toBeInTheDocument()
})

it('shows a retained parent limit separately from invalid report evidence', async () => {
  vi.spyOn(localApi, 'reportPlanAdjustments').mockResolvedValue([{ ...saved, revision: 2, operation: 'approve', source_current: false,
    authority: 'retained_parent_safety_guard', requires_review: true, feedback_source_current: false }])
  render(<ReportPlanAdjustments childId="a" snapshotId="report" feedbackRevision={null} />)
  open()
  expect(await screen.findByText(/这项时长上限仍保守生效/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '撤回此上限' })).toBeEnabled()
  expect(screen.queryByRole('button', { name: '保存待确认调整' })).not.toBeInTheDocument()
})
