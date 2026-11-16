import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { VariantGenerationStatus } from './VariantGenerationStatus'
import type { VariantGenerationJob } from './types'

const job: VariantGenerationJob = { job_id: 'job-1', status: 'blocked', error_code: 'GENERATION_OUTCOME_UNKNOWN', generation_mode: 'model', candidate_count: 0 }
afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear() })

it('retains the same retry command across a lost response and remount', async () => {
  vi.spyOn(localApi, 'variantGenerationJobs').mockResolvedValue([job])
  const retry = vi.spyOn(localApi, 'retryVariantGeneration').mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce({ ...job, status: 'pending' })
  const updated = vi.fn()
  const view = render(<VariantGenerationStatus childId="child-a" onUpdated={updated} />)
  fireEvent.click(await screen.findByRole('button', { name: '重新准备候选题' }))
  await screen.findByText('未确认重试结果，原重试指令已保留。')
  const action = retry.mock.calls[0][2]
  expect(localStorage.getItem('hsc-variant-retry:child-a:job-1')).toBe(action)
  view.unmount()
  render(<VariantGenerationStatus childId="child-a" onUpdated={updated} />)
  fireEvent.click(await screen.findByRole('button', { name: '重新准备候选题' }))
  await waitFor(() => expect(retry).toHaveBeenLastCalledWith('child-a', 'job-1', action))
  await screen.findByText('重试已保存，后台将继续准备题目。')
  expect(localStorage.getItem('hsc-variant-retry:child-a:job-1')).toBeNull()
  expect(updated).toHaveBeenCalledTimes(1)
})

it('does not offer a model retry for invalidated source material', async () => {
  vi.spyOn(localApi, 'variantGenerationJobs').mockResolvedValue([{ ...job, error_code: 'SUPPLY_SOURCE_CHANGED' }])
  render(<VariantGenerationStatus childId="child-a" onUpdated={() => {}} />)
  await screen.findByText(/来源题目需要重新核对/)
  expect(screen.queryByRole('button', { name: '重新准备候选题' })).not.toBeInTheDocument()
})

it('mounts one exact paper authorization for repeated jobs and retains access after generation completes', async () => {
  const paper = { ...job, child_id: 'child-a', mistake_id: 'mistake-a', error_code: 'PAPER_VARIANT_JOB_AUTHORIZATION_REQUIRED',
    source_snapshot_json: JSON.stringify({ source_ref: 'confirmed_paper_question_v1:artifact#candidate' }) }
  vi.spyOn(localApi, 'variantGenerationJobs').mockResolvedValue([paper, { ...paper, job_id: 'completed-job', status: 'completed', error_code: null }])
  const request = vi.fn().mockResolvedValue({ child_id: 'child-a', mistake_id: 'mistake-a', status: 'authorized', revision: 1,
    question_preview: '来自真实指定错题的单道题文预览。', question_version: 'q1', source_version: 's1', authorization_id: 'event1',
    message: '本题用途已授权。', reason: null, license_options: ['original_author'], local_only: true, cloud_transmission_allowed: false,
    rights_review_authority: 'parent_declaration_not_legal_or_network_verification' })
  render(<VariantGenerationStatus childId="child-a" onUpdated={vi.fn()} request={request} />)
  await screen.findByText('来自真实指定错题的单道题文预览。')
  expect(request).toHaveBeenCalledOnce()
  expect(request.mock.calls[0][0]).toBe('/v1/parent/children/child-a/paper-variant-authorizations/mistake-a')
  expect(screen.queryByRole('button', { name: '重新准备候选题' })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '撤回变式用途授权' })).toBeInTheDocument()
})
it('does not request another child paper permission or guess a missing mistake id', async () => {
  vi.spyOn(localApi, 'variantGenerationJobs').mockResolvedValue([
    { ...job, child_id: 'child-b', mistake_id: 'private-mistake', error_code: 'PAPER_VARIANT_JOB_AUTHORIZATION_REQUIRED' },
    { ...job, job_id: 'missing-id', error_code: 'PAPER_VARIANT_JOB_AUTHORIZATION_REQUIRED' }
  ] as VariantGenerationJob[])
  const request = vi.fn()
  render(<VariantGenerationStatus childId="child-a" onUpdated={vi.fn()} request={request} />)
  await screen.findByText(/准备任务 1/)
  expect(request).not.toHaveBeenCalled()
  expect(screen.queryByRole('button', { name: '重新准备候选题' })).not.toBeInTheDocument()
})
it('preserves explicit model-failure retry for paper jobs subject to the existing server authorization guard', async () => {
  const paper = { ...job, child_id: 'child-a', mistake_id: 'mistake-a',
    source_snapshot_json: JSON.stringify({ source_ref: 'confirmed_paper_question_v1:artifact#candidate' }) }
  vi.spyOn(localApi, 'variantGenerationJobs').mockResolvedValue([paper])
  const retry = vi.spyOn(localApi, 'retryVariantGeneration').mockResolvedValue({ ...paper, status: 'pending' })
  const request = vi.fn().mockResolvedValue({ child_id: 'child-a', mistake_id: 'mistake-a', status: 'authorized', revision: 1,
    question_preview: '已授权的合成原题，仍须由服务核对每次重试。', question_version: 'q1', source_version: 's1', authorization_id: 'event1',
    message: '本题用途已授权。', reason: null, license_options: ['original_author'], local_only: true, cloud_transmission_allowed: false,
    rights_review_authority: 'parent_declaration_not_legal_or_network_verification' })
  render(<VariantGenerationStatus childId="child-a" onUpdated={vi.fn()} request={request} />)
  await screen.findByText('已授权的合成原题，仍须由服务核对每次重试。')
  fireEvent.click(screen.getByRole('button', { name: '重新准备候选题' }))
  await waitFor(() => expect(retry).toHaveBeenCalledOnce())
  expect(request.mock.calls.filter(c => c[1]?.method === 'POST')).toHaveLength(0)
})

it.each(['success', 'failure'])('resets busy on child switch and fences a late %s while the next child retries', async outcome => {
  const a = { ...job, job_id: 'job-a' }, b = { ...job, job_id: 'job-b' }
  vi.spyOn(localApi, 'variantGenerationJobs').mockImplementation(async child => child === 'child-a' ? [a] : [b])
  let resolveA!: (value: VariantGenerationJob) => void, rejectA!: (reason: Error) => void, resolveB!: (value: VariantGenerationJob) => void
  vi.spyOn(localApi, 'retryVariantGeneration').mockImplementationOnce(() => new Promise((resolve, reject) => { resolveA = resolve; rejectA = reject }))
    .mockImplementationOnce(() => new Promise(resolve => { resolveB = resolve }))
  const firstUpdated = vi.fn(), secondUpdated = vi.fn()
  const mounted = render(<VariantGenerationStatus childId="child-a" onUpdated={firstUpdated} />)
  fireEvent.click(await screen.findByRole('button', { name: '重新准备候选题' }))
  expect(screen.getByRole('button', { name: '刷新准备状态' })).toBeDisabled()
  mounted.rerender(<VariantGenerationStatus childId="child-b" onUpdated={secondUpdated} />)
  await waitFor(() => expect(screen.getByRole('button', { name: '重新准备候选题' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '重新准备候选题' }))
  await act(async () => { if (outcome === 'success') resolveA({ ...a, status: 'pending' }); else rejectA(new Error('old child failed')) })
  expect(screen.getByRole('button', { name: '刷新准备状态' })).toBeDisabled()
  expect(screen.queryByText('重试已保存，后台将继续准备题目。')).not.toBeInTheDocument()
  expect(firstUpdated).not.toHaveBeenCalled(); expect(secondUpdated).not.toHaveBeenCalled()
  await act(async () => resolveB({ ...b, status: 'pending' }))
  await screen.findByText('重试已保存，后台将继续准备题目。')
  expect(screen.getByRole('button', { name: '刷新准备状态' })).toBeEnabled()
  expect(secondUpdated).toHaveBeenCalledOnce()
})

it.each(['sync', 'async'])('keeps a successful retry acknowledged when the %s parent refresh fails', async mode => {
  vi.spyOn(localApi, 'variantGenerationJobs').mockResolvedValue([job])
  const retry = vi.spyOn(localApi, 'retryVariantGeneration').mockResolvedValue({ ...job, status: 'pending' })
  const updated = vi.fn(() => { if (mode === 'sync') throw new Error('parent refresh'); return Promise.reject(new Error('parent refresh')) })
  render(<VariantGenerationStatus childId="child-a" onUpdated={updated} />)
  fireEvent.click(await screen.findByRole('button', { name: '重新准备候选题' }))
  await screen.findByText('重试已保存，但候选列表未刷新；请刷新查看，不要重复提交重试。')
  expect(localStorage.getItem('hsc-variant-retry:child-a:job-1')).toBeNull()
  expect(screen.getByRole('button', { name: '刷新准备状态' })).toBeEnabled()
  expect(retry).toHaveBeenCalledOnce()
})

it('handles a rejected readonly refresh callback without issuing or stranding a retry', async () => {
  vi.spyOn(localApi, 'variantGenerationJobs').mockResolvedValue([job])
  const retry = vi.spyOn(localApi, 'retryVariantGeneration')
  render(<VariantGenerationStatus childId="child-a" onUpdated={vi.fn().mockRejectedValue(new Error('parent read failed'))} />)
  fireEvent.click(await screen.findByRole('button', { name: '刷新准备状态' }))
  await screen.findByText('准备状态或候选列表未刷新，请重试读取；没有重新准备题目。')
  expect(screen.getByRole('button', { name: '刷新准备状态' })).toBeEnabled()
  expect(retry).not.toHaveBeenCalled()
})

it.each(['blocked', 'failed', 'completed'] as const)('reports current %s on an old retry receipt without promising continued execution', async status => {
  vi.spyOn(localApi, 'variantGenerationJobs').mockResolvedValue([job])
  const retry = vi.spyOn(localApi, 'retryVariantGeneration').mockResolvedValue({ ...job, status })
  const key = 'hsc-variant-retry:child-a:job-1'
  localStorage.setItem(key, 'original-retry-command')
  render(<VariantGenerationStatus childId="child-a" onUpdated={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: '重新准备候选题' }))
  await screen.findByText(status === 'completed' ? '原重试回执已核对；准备已结束，候选仍须审核。'
    : status === 'blocked' ? '原重试回执已核对；任务当前仍需处理，不会据旧回执自动继续。'
      : '原重试回执已核对；任务当前准备失败，请先核对原因。')
  expect(screen.queryByText('重试已保存，后台将继续准备题目。')).not.toBeInTheDocument()
  expect(retry).toHaveBeenCalledExactlyOnceWith('child-a', 'job-1', 'original-retry-command')
  expect(localStorage.getItem(key)).toBeNull()
  expect(screen.getByRole('button', { name: '刷新准备状态' })).toBeEnabled()
})

it.each(['getItem', 'setItem'] as const)('does not claim saved recovery or issue a request when storage %s fails', async method => {
  vi.spyOn(localApi, 'variantGenerationJobs').mockResolvedValue([job])
  const retry = vi.spyOn(localApi, 'retryVariantGeneration')
  render(<VariantGenerationStatus childId="child-a" onUpdated={vi.fn()} />)
  const button = await screen.findByRole('button', { name: '重新准备候选题' })
  vi.spyOn(Storage.prototype, method).mockImplementationOnce(() => { throw new Error('storage unavailable') })
  fireEvent.click(button)
  await screen.findByText('本机无法保存重试指令，未发送请求。')
  expect(screen.queryByText('未确认重试结果，原重试指令已保留。')).not.toBeInTheDocument()
  expect(retry).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: '刷新准备状态' })).toBeEnabled()
})
