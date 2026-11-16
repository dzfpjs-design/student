import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { PeriodReportRebinding } from './PeriodReportRebinding'
import { rebindingApi, clearPeriodRebindingLocalState, readRebindingLocal } from './reportRebindingContract'
import type { RebindingAdjustment, RebindingContext } from './reportRebindingContract'
import { ApiError } from './api'

const context = {
  binding: { version: 'v1', child_id: 'child-a', feedback_event_id: 'event-a', feedback_revision: 1,
    experience_hash: 'feedback-proof', historical: { snapshot_id: 'old-a' }, target: { snapshot_id: 'new-b' } },
  feedback: { snapshot_id: 'old-a', notes: '原反馈：压力偏高', helpfulness: 2, stress_rating: 5, revision: 1, created_at: '2026-09-10T10:00:00Z' },
  historical_report: { snapshot_id: 'old-a', content: { headline: '原报告标题' }, parent_edit: {} },
  report: { snapshot_id: 'new-b', content: { headline: '已纳入反馈的新版报告', summary: { interpretation: '尚不足以判断稳定掌握' }, parent_coaching: { action: { what: '只核对一步' }, script: ['先听孩子的选择'] } }, parent_edit: { parent_note: '家长已核对新版内容' } },
} as unknown as RebindingContext
const proposal: RebindingAdjustment = {
  adjustment_id: 'bound-cap', snapshot_id: 'new-b', revision: 1, operation: 'propose', review_hash: 'exact-review',
  source_current: true, requires_review: false, effective_as_guard: false, release_reason: null,
  binding: context.binding, terms: { constraint_type: 'daily_total_minutes_cap', value_minutes: 20, start_date: '2026-09-12', end_date: '2026-09-12' }, evidence: context,
}
beforeEach(() => {
  localStorage.clear(); vi.restoreAllMocks()
  vi.spyOn(rebindingApi, 'contexts').mockResolvedValue({ items: [context] })
  vi.spyOn(rebindingApi, 'list').mockResolvedValue({ items: [] })
})
afterEach(cleanup)
async function open(): Promise<void> {
  fireEvent.click(screen.getByText('根据历史反馈确认后续上限'))
  await screen.findByText('原反馈：压力偏高')
}

test('parent sees original feedback and reviewed successor, then explicitly confirms exact terms', async () => {
  vi.spyOn(rebindingApi, 'propose').mockImplementation(async () => {
    expect(readRebindingLocal('child-a')?.pending?.key).toBeTruthy()
    return proposal
  })
  const review = vi.spyOn(rebindingApi, 'review').mockResolvedValue({ ...proposal, revision: 2, operation: 'approve', effective_as_guard: true })
  render(<PeriodReportRebinding childId="child-a" snapshotId="new-b" />)
  await open()
  expect(screen.getByText(/家长已核对新版内容/)).toBeVisible()
  fireEvent.click(screen.getByText('保存关联提案'))
  const confirm = await screen.findByText('确认此上限')
  expect(confirm).toBeDisabled()
  await waitFor(() => expect(screen.getByLabelText('我已核对原反馈、当前报告及上述日期和上限')).toBeEnabled())
  fireEvent.click(screen.getByLabelText('我已核对原反馈、当前报告及上述日期和上限'))
  await waitFor(() => expect(confirm).toBeEnabled())
  fireEvent.click(confirm)
  await waitFor(() => expect(review).toHaveBeenCalledWith('child-a', proposal, 'approve', true, expect.any(String)))
  expect(await screen.findByText(/确认已保存/)).toBeVisible()
  expect(readRebindingLocal('child-a')?.pending).toBeNull()
})

test('lost response survives remount and resolves with GET original key, never another POST', async () => {
  const write = vi.spyOn(rebindingApi, 'propose').mockRejectedValue(new Error('response lost after commit'))
  const get = vi.spyOn(rebindingApi, 'command').mockResolvedValue({ receipt: proposal, current: proposal })
  const view = render(<PeriodReportRebinding childId="child-a" snapshotId="new-b" />)
  await open(); fireEvent.click(screen.getByText('保存关联提案'))
  await screen.findByText(/保存结果尚未确认/)
  const key = readRebindingLocal('child-a')!.pending!.key
  view.unmount()
  render(<PeriodReportRebinding childId="child-a" snapshotId="new-b" />)
  await screen.findByText('核对原操作')
  await waitFor(() => expect(screen.getByText('核对原操作')).not.toBeDisabled())
  fireEvent.click(screen.getByText('核对原操作'))
  await screen.findByText(/没有重复提交/)
  expect(get).toHaveBeenCalledExactlyOnceWith('child-a', key)
  expect(write).toHaveBeenCalledTimes(1)
})

test('unknown GET keeps identity until explicit cancel is acknowledged', async () => {
  vi.spyOn(rebindingApi, 'propose').mockRejectedValue(new Error('not delivered'))
  vi.spyOn(rebindingApi, 'command').mockRejectedValue(new ApiError('not found', 404))
  const cancel = vi.spyOn(rebindingApi, 'cancel').mockResolvedValue({ status: 'cancelled_not_committed' })
  render(<PeriodReportRebinding childId="child-a" snapshotId="new-b" />)
  await open(); fireEvent.click(screen.getByText('保存关联提案'))
  await screen.findByText(/保存结果尚未确认/)
  const key = readRebindingLocal('child-a')!.pending!.key
  fireEvent.click(screen.getByText('核对原操作'))
  await screen.findByText(/尚未查到确定结果/)
  expect(readRebindingLocal('child-a')!.pending!.key).toBe(key)
  fireEvent.click(screen.getByText('取消未完成提交'))
  await screen.findByText(/迟到请求不会执行/)
  expect(cancel).toHaveBeenCalledExactlyOnceWith('child-a', key, 'new-b')
})

test('storage failure prevents any proposal; corrupted recovery never sends automatically', async () => {
  const write = vi.spyOn(rebindingApi, 'propose')
  render(<PeriodReportRebinding childId="child-a" snapshotId="new-b" />)
  await open()
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
  fireEvent.click(screen.getByText('保存关联提案'))
  await screen.findByText('本机未能保存核对编号，未发送。')
  expect(write).not.toHaveBeenCalled()
})

test('late old-child response stays out of new child and old recovery remains durable', async () => {
  let finish!: (value: RebindingAdjustment) => void
  vi.spyOn(rebindingApi, 'propose').mockImplementation(() => new Promise(resolve => { finish = resolve }))
  const view = render(<PeriodReportRebinding childId="child-a" snapshotId="new-b" />)
  await open(); fireEvent.click(screen.getByText('保存关联提案'))
  view.rerender(<PeriodReportRebinding childId="child-b" snapshotId="other-report" />)
  await act(async () => finish(proposal))
  expect(screen.queryByText('原反馈：压力偏高')).not.toBeInTheDocument()
  expect(readRebindingLocal('child-a')?.pending).toBeTruthy()
  expect(readRebindingLocal('child-b')).toBeNull()
  clearPeriodRebindingLocalState('child-b')
  expect(readRebindingLocal('child-a')?.pending).toBeTruthy()
  clearPeriodRebindingLocalState()
  expect(readRebindingLocal('child-a')).toBeNull()
})

test('changed source disables confirmation while rejection remains available', async () => {
  vi.spyOn(rebindingApi, 'list').mockResolvedValue({ items: [{ ...proposal, source_current: false }] })
  render(<PeriodReportRebinding childId="child-a" snapshotId="new-b" />)
  fireEvent.click(screen.getByText('根据历史反馈确认后续上限'))
  const record = (await screen.findByText('确认此上限')).closest('li')!
  expect(within(record).getByText('确认此上限')).toBeDisabled()
  expect(within(record).getByText('不采用')).not.toBeDisabled()
})

test('recovering on another report retains a visible result and opens its original report', async () => {
  vi.spyOn(rebindingApi, 'propose').mockRejectedValue(new Error('lost'))
  vi.spyOn(rebindingApi, 'command').mockResolvedValue({ receipt: proposal, current: proposal })
  const navigate = vi.fn()
  const view = render(<PeriodReportRebinding childId="child-a" snapshotId="new-b" onOpenReport={navigate} />)
  await open(); fireEvent.click(screen.getByText('保存关联提案'))
  await screen.findByText(/保存结果尚未确认/)
  view.rerender(<PeriodReportRebinding childId="child-a" snapshotId="different-report" onOpenReport={navigate} />)
  await waitFor(() => expect(screen.getByText('核对原操作')).not.toBeDisabled())
  fireEvent.click(screen.getByText('核对原操作'))
  fireEvent.click(await screen.findByText('查看原操作所在报告'))
  expect(navigate).toHaveBeenCalledExactlyOnceWith('new-b')
  expect(screen.getByText(/原操作属于另一份报告/)).toBeVisible()
})
