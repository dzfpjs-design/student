import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ApiError, localApi } from './api'
import { ExecutionTracePanel } from './ExecutionTracePanel'
import type { ExecutionTraceDetail, ExecutionTraceTurn } from './executionTrace'

const turn: ExecutionTraceTurn = { turn_id: 'private-turn', created_at: '2026-09-07T12:00:00Z', status: 'unknown', error_code: 'DEVICE_COMMAND_TIMEOUT', capability_name: 'task.pause', trace_event_count: 2 }
const detail: ExecutionTraceDetail = { child_id: 'a', turn_id: turn.turn_id, spans: [{ event_id: 'private-event', event_type: 'action_result',
  source_status: 'unknown', error_code: 'DEVICE_COMMAND_TIMEOUT', elapsed_ms: 1500, source_current: true, parent_link_current: false,
  recovery_action: 'check_receipt_before_retry' }], existing_steps: [{ step_id: 'private-step', status: 'unknown', error_code: null }],
  missing_stages: ['learning_result'], local_only: true, cloud_transmission_allowed: false, outcome_evidence_created: false }
afterEach(() => { cleanup(); vi.restoreAllMocks() })
const open = (): void => { fireEvent.click(screen.getByText('交互与执行记录')) }

it('reads lazily and explains unknown results without offering command replay or internal IDs', async () => {
  const list = vi.spyOn(localApi, 'executionTraces').mockResolvedValue({ items: [turn], next_cursor: null })
  vi.spyOn(localApi, 'executionTrace').mockResolvedValue(detail)
  const execute = vi.spyOn(localApi, 'executeAgentTurn')
  render(<ExecutionTracePanel childId="a" />)
  expect(list).not.toHaveBeenCalled()
  open(); fireEvent.click(await screen.findByRole('button', { name: /暂停学习任务/ }))
  await screen.findByText('先核对已有结果，避免重复执行。')
  expect(screen.getByText('前序记录已不可用。')).toBeInTheDocument()
  expect(screen.getByText(/不能单独证明学习改善/)).toBeInTheDocument()
  expect(screen.queryByText(/private-/)).not.toBeInTheDocument()
  expect(execute).not.toHaveBeenCalled()
})

it('shows missing traces as unknown and paginates without duplicating records', async () => {
  const list = vi.spyOn(localApi, 'executionTraces').mockResolvedValueOnce({ items: [{ ...turn, trace_event_count: 0 }], next_cursor: 'page-2' })
    .mockResolvedValue({ items: [turn, { ...turn, turn_id: 'second', capability_name: 'task.start' }], next_cursor: null })
  vi.spyOn(localApi, 'executionTrace').mockResolvedValue({ ...detail, spans: [], existing_steps: [] })
  render(<ExecutionTracePanel childId="a" />)
  open(); await screen.findByText('过程记录缺失')
  fireEvent.click(screen.getByRole('button', { name: '较早记录' }))
  await screen.findByRole('button', { name: /开始学习任务/ })
  expect(list.mock.calls[1]).toEqual(['a', 'page-2'])
  expect(screen.getAllByRole('button', { name: /暂停学习任务/ })).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: /暂停学习任务/ }))
  await screen.findByText('没有可用的过程记录，不能据此判断哪一步成功。')
})

it('ignores a previous child late detail and clears selection', async () => {
  vi.spyOn(localApi, 'executionTraces').mockResolvedValueOnce({ items: [turn], next_cursor: null }).mockResolvedValue({ items: [], next_cursor: null })
  let finish!: (result: ExecutionTraceDetail) => void
  vi.spyOn(localApi, 'executionTrace').mockImplementation(() => new Promise(done => { finish = done }))
  const view = render(<ExecutionTracePanel childId="a" />)
  open(); fireEvent.click(await screen.findByRole('button', { name: /暂停学习任务/ }))
  await waitFor(() => expect(localApi.executionTrace).toHaveBeenCalledTimes(1))
  view.rerender(<ExecutionTracePanel childId="b" />)
  await act(async () => finish(detail))
  open(); await screen.findByText('该孩子还没有交互记录。')
  expect(screen.queryByLabelText('执行过程')).not.toBeInTheDocument()
})

it('rejects mismatched source responses and supports read-only error recovery', async () => {
  vi.spyOn(localApi, 'executionTraces').mockResolvedValue({ items: [turn], next_cursor: null })
  vi.spyOn(localApi, 'executionTrace').mockResolvedValueOnce({ ...detail, child_id: 'b' })
    .mockRejectedValueOnce(new ApiError('deleted', 404)).mockResolvedValue(detail)
  render(<ExecutionTracePanel childId="a" />)
  open(); fireEvent.click(await screen.findByRole('button', { name: /暂停学习任务/ }))
  await screen.findByText(/返回记录与当前交互不一致/)
  fireEvent.click(screen.getByRole('button', { name: '重试查询执行过程' }))
  await screen.findByText(/这条交互记录已不可用/)
  fireEvent.click(screen.getByRole('button', { name: '重试查询执行过程' }))
  await screen.findByText('先核对已有结果，避免重复执行。')
})

it('makes expired parent authorization explicit instead of an empty history', async () => {
  vi.spyOn(localApi, 'executionTraces').mockRejectedValue(new ApiError('expired', 401))
  render(<ExecutionTracePanel childId="a" />)
  open(); await screen.findByText('家长验证已过期，请重新打开家长中心。')
  expect(screen.queryByText('该孩子还没有交互记录。')).not.toBeInTheDocument()
})
