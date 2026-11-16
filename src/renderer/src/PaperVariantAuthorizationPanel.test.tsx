import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { PaperVariantAuthorizationPanel } from './PaperVariantAuthorizationPanel'
import { clearPaperVariantAuthorizationLocalState, paperVariantAuthorizationKey } from './paperVariantAuthorizationApi'

const preview = '一根绳子长24米，平均分成3段，每段长多少米？'
const view = (patch = {}) => ({ child_id: 'child-a', mistake_id: 'mistake-a', revision: 0, status: 'authorization_required',
  reason: null, message: '请核对这道原题。', question_preview: preview, question_version: 'q1', source_version: 's1', authorization_id: null,
  license_options: ['CC0-1.0', 'original_author', 'explicit_cloud_processing_permission'],
  rights_review_authority: 'parent_declaration_not_legal_or_network_verification', local_only: true, cloud_transmission_allowed: false, ...patch })
const receipt = (patch = {}) => ({ local_only: true, cloud_transmission_allowed: false,
  receipt: { event_id: 'event1', revision: 1, command: 'grant' }, current: view({ revision: 1, status: 'authorized', authorization_id: 'event1' }),
  generation_job: { job_id: 'job1', status: 'pending', requested_count: 4, error_code: null }, ...patch })
const props = { childId: 'child-a', mistakeId: 'mistake-a' }
const key = paperVariantAuthorizationKey('child-a', 'mistake-a')
async function decide() {
  await screen.findByText(preview)
  fireEvent.change(screen.getByLabelText('权利依据'), { target: { value: 'original_author' } })
  fireEvent.change(screen.getByLabelText('许可链接或声明名称'), { target: { value: '原创声明' } })
  fireEvent.change(screen.getByLabelText('权利依据说明（至少20字）'), { target: { value: '我是这道隔离合成题目的原创作者，仅声明本题题文的变式处理用途。' } })
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: '确认授权并准备变式' }))
}
afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks() })
it('binds the visible original, creates one job through grant and survives a read refresh', async () => {
  const request = vi.fn().mockResolvedValueOnce(view()).mockResolvedValueOnce(receipt()).mockResolvedValueOnce(receipt().current)
  const onChanged = vi.fn()
  render(<PaperVariantAuthorizationPanel {...props} request={request} onChanged={onChanged} />)
  await decide(); await screen.findByText('当前已授权题文变式用途')
  expect(request.mock.calls[1][0]).toMatch(/mistake-a\/grant$/)
  const body = JSON.parse(request.mock.calls[1][1].body)
  expect(body).toMatchObject({ expected_revision: 0, question_version: 'q1', source_version: 's1', license_basis: { license_id: 'original_author' } })
  expect(Object.keys(body).sort()).toEqual(['client_action_id', 'expected_revision', 'license_basis', 'question_version', 'source_version'])
  expect(localStorage.getItem(key)).toBeNull(); expect(onChanged).toHaveBeenCalledOnce()
  fireEvent.click(screen.getByRole('button', { name: '刷新变式用途授权' }))
  await screen.findByText('当前已授权题文变式用途')
  expect(request.mock.calls.filter(c => c[1]?.method === 'POST')).toHaveLength(1)
})
it('reopens a lost response without sending, retries the exact key and respects current revoked over old receipt', async () => {
  const request = vi.fn().mockResolvedValueOnce(view()).mockRejectedValueOnce(new Error('lost'))
    .mockResolvedValueOnce(view({ revision: 2, status: 'revoked' })).mockResolvedValueOnce(receipt({ current: view({ revision: 2, status: 'revoked' }),
      generation_job: { job_id: 'job1', status: 'blocked', requested_count: 4, error_code: 'PAPER_VARIANT_AUTHORIZATION_REVOKED' } }))
  const first = render(<PaperVariantAuthorizationPanel {...props} request={request} />)
  await decide(); await screen.findByRole('alert'); const body = request.mock.calls[1][1].body
  first.unmount(); render(<PaperVariantAuthorizationPanel {...props} request={request} />)
  await screen.findByText('当前变式用途已撤回')
  expect(request).toHaveBeenCalledTimes(3)
  fireEvent.click(screen.getByRole('button', { name: '重试原授权请求' }))
  await waitFor(() => expect(localStorage.getItem(key)).toBeNull())
  expect(request.mock.calls[3][1].body).toBe(body)
  expect(screen.queryByText('当前已授权题文变式用途')).not.toBeInTheDocument()
  expect(screen.getByText(/历史回执不会恢复/)).toBeInTheDocument()
})
it('clears the question immediately after a current read returns ownership 404', async () => {
  const request = vi.fn().mockResolvedValueOnce(view()).mockRejectedValueOnce(new ApiError('not found', 404))
  render(<PaperVariantAuthorizationPanel {...props} request={request} />)
  await screen.findByText(preview)
  fireEvent.click(screen.getByRole('button', { name: '刷新变式用途授权' }))
  await screen.findByText('原题不存在或不属于当前孩子，不能沿用旧预览。')
  expect(screen.queryByText(preview)).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '确认授权并准备变式' })).not.toBeInTheDocument()
})
it('fences another child and a late successful grant after a privacy clear', async () => {
  let resolve!: (value: unknown) => void
  const request = vi.fn().mockResolvedValueOnce(view()).mockImplementationOnce(() => new Promise<unknown>(r => { resolve = r }))
    .mockResolvedValue(view({ status: 'blocked', question_preview: null }))
  render(<PaperVariantAuthorizationPanel {...props} request={request} />)
  await decide(); expect(localStorage.getItem(key)).not.toBeNull()
  const other = paperVariantAuthorizationKey('child-b', 'm-b'); localStorage.setItem(other, 'keep')
  act(() => clearPaperVariantAuthorizationLocalState('child-a'))
  await act(async () => resolve(receipt()))
  expect(localStorage.getItem(key)).toBeNull(); expect(localStorage.getItem(other)).toBe('keep')
  expect(screen.queryByText('当前已授权题文变式用途')).not.toBeInTheDocument()
})
it('does not treat a parent refresh failure as a failed grant or restore the pending command', async () => {
  const request = vi.fn().mockResolvedValueOnce(view()).mockResolvedValueOnce(receipt())
  render(<PaperVariantAuthorizationPanel {...props} request={request} onChanged={vi.fn().mockRejectedValue(new Error('refresh failed'))} />)
  await decide(); await screen.findByText(/授权操作已保存，但准备列表未刷新/)
  expect(localStorage.getItem(key)).toBeNull()
  expect(screen.queryByRole('button', { name: '重试原授权请求' })).not.toBeInTheDocument()
})
it('keeps blocked source and malformed recovery data unsendable', async () => {
  localStorage.setItem(key, '{broken')
  const request = vi.fn().mockResolvedValue(view({ status: 'blocked', reason: 'PAPER_VARIANT_SOURCE_CHANGED' }))
  render(<PaperVariantAuthorizationPanel {...props} request={request} />)
  await screen.findByText(preview)
  expect(screen.queryByRole('button', { name: '确认授权并准备变式' })).not.toBeInTheDocument()
  expect(request.mock.calls.filter(c => c[1]?.method === 'POST')).toHaveLength(0)
})
it('uses the current revision to revoke, then reloads the persisted revoked state', async () => {
  const revoked = view({ revision: 2, status: 'revoked' })
  const request = vi.fn().mockResolvedValueOnce(receipt().current).mockResolvedValueOnce(receipt({
    receipt: { event_id: 'event2', revision: 2, command: 'revoke' }, current: revoked, generation_job: null })).mockResolvedValueOnce(revoked)
  const first = render(<PaperVariantAuthorizationPanel {...props} request={request} />)
  fireEvent.click(await screen.findByRole('button', { name: '撤回变式用途授权' }))
  await screen.findByText('当前变式用途已撤回')
  expect(JSON.parse(request.mock.calls[1][1].body)).toEqual({ expected_revision: 1, client_action_id: expect.any(String) })
  first.unmount(); render(<PaperVariantAuthorizationPanel {...props} request={request} />)
  await screen.findByText('当前变式用途已撤回')
  expect(request.mock.calls.filter(c => c[1]?.method === 'POST')).toHaveLength(1)
})
it('discards a draft after source change and an old pending after source invalidation', async () => {
  const request = vi.fn().mockResolvedValueOnce(view()).mockResolvedValueOnce(view({ source_version: 's2' }))
    .mockRejectedValueOnce(new Error('lost')).mockResolvedValueOnce(view({ status: 'invalidated', question_preview: null, source_version: null }))
  render(<PaperVariantAuthorizationPanel {...props} request={request} />)
  await screen.findByText(preview)
  fireEvent.change(screen.getByLabelText('许可链接或声明名称'), { target: { value: 'old draft' } })
  fireEvent.click(screen.getByRole('button', { name: '刷新变式用途授权' }))
  await waitFor(() => expect(screen.getByLabelText('许可链接或声明名称')).toHaveValue(''))
  await decide(); await screen.findByRole('alert')
  fireEvent.click(screen.getByRole('button', { name: '刷新变式用途授权' }))
  await screen.findByText('当前题文用途不可用')
  expect(localStorage.getItem(key)).toBeNull()
  expect(screen.queryByRole('button', { name: '重试原授权请求' })).not.toBeInTheDocument()
})
it('does not publish a delayed previous child read into the new child', async () => {
  let resolve!: (value: unknown) => void
  const request = vi.fn().mockImplementationOnce(() => new Promise<unknown>(r => { resolve = r }))
    .mockResolvedValueOnce(view({ child_id: 'child-b', mistake_id: 'mistake-b', question_preview: '乙孩子专属原题文字，不属于甲孩子。' }))
  const mounted = render(<PaperVariantAuthorizationPanel {...props} request={request} />)
  mounted.rerender(<PaperVariantAuthorizationPanel childId="child-b" mistakeId="mistake-b" request={request} />)
  await screen.findByText('乙孩子专属原题文字，不属于甲孩子。')
  await act(async () => resolve(view()))
  expect(screen.queryByText(preview)).not.toBeInTheDocument()
})
it('relocks on auth expiry and removes the old preview', async () => {
  const request = vi.fn().mockResolvedValueOnce(view()).mockRejectedValueOnce(new ApiError('expired', 401)), relock = vi.fn()
  render(<PaperVariantAuthorizationPanel {...props} request={request} onRequireParentAuth={relock} />)
  await screen.findByText(preview)
  fireEvent.click(screen.getByRole('button', { name: '刷新变式用途授权' }))
  fireEvent.click(await screen.findByRole('button', { name: '重新解锁家长操作' }))
  expect(relock).toHaveBeenCalledOnce(); expect(screen.queryByText(preview)).not.toBeInTheDocument()
})
it('will not send when the durable command cannot be saved', async () => {
  const request = vi.fn().mockResolvedValue(view())
  render(<PaperVariantAuthorizationPanel {...props} request={request} />)
  await screen.findByText(preview)
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
  fireEvent.change(screen.getByLabelText('许可链接或声明名称'), { target: { value: 'original declaration' } })
  await screen.findByText('本机无法保存恢复记录，未发送新的授权操作。')
  expect(screen.getByRole('button', { name: '确认授权并准备变式' })).toBeDisabled()
  expect(request.mock.calls.filter(c => c[1]?.method === 'POST')).toHaveLength(0)
})
