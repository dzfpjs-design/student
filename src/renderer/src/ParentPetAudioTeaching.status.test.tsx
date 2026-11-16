import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ParentPetAudioTeaching } from './ParentPetAudioTeaching'

beforeEach(() => localStorage.clear())
afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear() })
async function show(work: Record<string, unknown>) {
  const request = vi.fn(async (path: string, init?: RequestInit): Promise<unknown> => {
    if (init?.method === 'POST') throw Error('Display contract must not send requests')
    return path.endsWith('/authorization')
      ? { child_id: 'primary', configured: true, current: true, grant: null }
      : { child_id: 'primary', items: [{ work_id: 'work-1', child_id: 'primary', generation: 1,
          question: 'Tell me about your school.', created_at: '2026-09-11T12:00:00Z', ...work }] }
  })
  render(<ParentPetAudioTeaching childId="primary" request={request as <T>(path: string, init?: RequestInit) => Promise<T>} />)
  await screen.findByText('此用途已获许可')
  return { request, row: within(screen.getByRole('listitem')) }
}

it.each(['pending', 'runtime_delivered', 'awaiting_receipt'])('unavailable source takes precedence over historical delivery=%s', async delivery_status => {
  const { row, request } = await show({ state: 'verification_pending', source_verification: 'unavailable', delivery_status })
  expect(row.getByText(/录音暂时无法读取，等待核验/)).toBeInTheDocument()
  expect(row.queryByText(/建议已交给学习伙伴/)).not.toBeInTheDocument()
  expect(row.queryByRole('button', { name: '确认重新发送' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '刷新原音教学状态' }))
  await screen.findByText(/录音暂时无法读取，等待核验/)
  expect(request.mock.calls.every(([, init]) => init?.method !== 'POST')).toBe(true)
})
it('saved result waiting for the exact runtime receipt is not displayed as delivered or offered for resend', async () => {
  const { row } = await show({ state: 'response_saved', source_verification: 'current', delivery_status: 'awaiting_receipt' })
  expect(row.getByText(/建议已保存，正在核对学习伙伴的原交付回执/)).toBeInTheDocument()
  expect(row.queryByText(/建议已交给学习伙伴/)).not.toBeInTheDocument()
  expect(row.queryByRole('button', { name: '确认重新发送' })).not.toBeInTheDocument()
})
it('retired saved suggestions remain historical rather than claiming current delivery', async () => {
  const { row } = await show({ state: 'response_saved', source_verification: 'current', delivery_status: 'retired' })
  expect(row.getByText(/建议保留为历史记录，已停止向当前学习交付/)).toBeInTheDocument()
  expect(row.queryByRole('button', { name: '确认重新发送' })).not.toBeInTheDocument()
})
it('only current source and confirmed runtime delivery display delivered', async () => {
  const { row } = await show({ state: 'delivered', source_verification: 'current', delivery_status: 'runtime_delivered' })
  expect(row.getByText(/建议已交给学习伙伴/)).toBeInTheDocument()
})
it.each([
  { state: 'delivered', source_verification: 'unavailable', delivery_status: 'runtime_delivered' },
  { state: 'delivered', source_verification: 'changed', delivery_status: 'runtime_delivered' },
  { state: 'delivered', source_verification: 'current', delivery_status: 'pending' },
  { state: 'delivered' }
])('does not infer current delivery from the old state alone: %j', async work => {
  const { row } = await show(work)
  expect(row.queryByText(/建议已交给学习伙伴/)).not.toBeInTheDocument()
  expect(row.queryByRole('button', { name: '确认重新发送' })).not.toBeInTheDocument()
})
it.each([
  { state: 'unknown', source_verification: 'unavailable', delivery_status: 'pending' },
  { state: 'failed', source_verification: 'current', delivery_status: 'awaiting_receipt' },
  { state: 'failed', source_verification: 'current', delivery_status: 'retired' },
  { state: 'unknown', source_verification: 'changed', delivery_status: 'pending' }
])('does not expose resend for unresolved or retired current results: %j', async work => {
  const { row, request } = await show(work)
  expect(row.queryByRole('button', { name: '确认重新发送' })).not.toBeInTheDocument()
  expect(row.queryByRole('checkbox')).not.toBeInTheDocument()
  expect(request.mock.calls.every(([, init]) => init?.method !== 'POST')).toBe(true)
})
it.each(['unknown', 'failed'])('keeps explicit charged retry confirmation for an eligible %s result', async state => {
  const { row, request } = await show({ state, source_verification: 'current', delivery_status: 'pending' })
  expect(row.getByRole('button', { name: '确认重新发送' })).toBeDisabled()
  fireEvent.click(row.getByRole('checkbox'))
  expect(row.getByRole('button', { name: '确认重新发送' })).toBeEnabled()
  expect(request.mock.calls.every(([, init]) => init?.method !== 'POST')).toBe(true)
})
it('current saved result with pending delivery accurately distinguishes persistence from delivery', async () => {
  const { row } = await show({ state: 'response_saved', source_verification: 'current', delivery_status: 'pending' })
  expect(row.getByText(/建议已保存，等待交给学习伙伴/)).toBeInTheDocument()
  expect(row.queryByRole('button', { name: '确认重新发送' })).not.toBeInTheDocument()
})
