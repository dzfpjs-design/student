import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { webcrypto } from 'node:crypto'
import { ApiError } from './api'
import { ParentVisualRetention } from './ParentVisualRetention'
import { createVisualRetentionApi, type RetentionView, type RetentionCommand, type RetentionResult } from './visualRetentionApi'

const initial: RetentionView = { child_id: 'a', local_only: true, cloud_transmission_allowed: false,
  policy: { purpose: 'local_derived_visual_retention_v1', status: 'disabled', revision: null, scope: null,
    retention_days: null, authorization_expires_at: null, max_bytes: null, used_bytes: 0, reason: 'VISUAL_RETENTION_NOT_ENABLED' },
  limits: { retention_days: [1,30], authorization_days: [1,365], max_bytes: [1048576,268435456] } }
function result(command: RetentionCommand): RetentionResult {
  return { ...initial, policy: { ...initial.policy, status: command.action === 'revoke' ? 'revoked' : 'enabled', revision: 'grant-1',
    scope: { child_id: 'a', session_id: null, session_state: 'study' }, retention_days: 7, max_bytes: 134217728,
    authorization_expires_at: '2026-10-10T12:00:00Z' },
    receipt: { receipt_id: 'grant-1', child_id: 'a', action: command.action, client_action_id: command.client_action_id,
      status: command.action === 'revoke' ? 'revoked' : command.action === 'update' ? 'updated' : 'enabled', revision: 'grant-1', request_hash: 'a'.repeat(64) }, replayed: false }
}
function api() {
  return { get: vi.fn(async () => initial), submit: vi.fn(async (_child: string, c: RetentionCommand) => result(c)) }
}
beforeEach(() => { localStorage.clear(); vi.stubGlobal('crypto', webcrypto) })
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
async function enable() {
  await screen.findByRole('checkbox'); fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: '开启自动保留' }))
}
it('persists an explicit child-only setup before dispatch, without per-question consent or cloud grant', async () => {
  const client = api()
  client.submit.mockImplementation(async (child, c) => {
    expect(JSON.parse(localStorage.getItem('hsc-visual-retention:a')!).command).toEqual(c)
    expect(child).toBe('a'); expect(c).toMatchObject({ action: 'enable', expected_revision: null, session_id: null })
    expect(c).not.toHaveProperty('cloud_transmission_allowed')
    return result(c)
  })
  render(<ParentVisualRetention childId="a" api={client} />)
  expect(await screen.findByRole('button', { name: '开启自动保留' })).toBeDisabled()
  expect(client.submit).not.toHaveBeenCalled()
  await enable(); await screen.findByText('学习裁图保留设置已保存。')
  expect(localStorage.getItem('hsc-visual-retention:a')).toBeNull()
  expect(screen.getByRole('button', { name: '更新保留设置' })).toBeDisabled()
})
it('reuses the original operation across lost response and remount, showing current revoked state', async () => {
  const client = api(); client.submit.mockRejectedValueOnce(Error('response lost'))
  const view = render(<ParentVisualRetention childId="a" api={client} />)
  await enable(); await screen.findByRole('alert')
  const original = client.submit.mock.calls[0][1]
  view.unmount()
  client.submit.mockImplementation(async (_child, c) => ({ ...result(c), replayed: true,
    policy: { ...result(c).policy, status: 'revoked' } }))
  render(<ParentVisualRetention childId="a" api={client} />)
  fireEvent.click(screen.getByRole('button', { name: '核对原设置操作' }))
  await screen.findByText('原操作已核对，以下为当前生效状态。')
  expect(client.submit.mock.calls[1][1]).toEqual(original)
  expect(screen.getByText(/已撤回 ·/)).toBeVisible()
  expect(screen.getByRole('button', { name: '开启自动保留' })).toBeDisabled()
})
it('does not send if the operation cannot be saved locally', async () => {
  const client = api(); render(<ParentVisualRetention childId="a" api={client} />)
  await screen.findByRole('checkbox')
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw Error('full') })
  await enable(); expect(client.submit).not.toHaveBeenCalled()
  expect(screen.getByText('本机无法保存操作恢复记录，本次设置尚未发送。')).toBeVisible()
})
it('releases only an explicitly refused old revision after reading current state, never reauthorizing', async () => {
  const client = api()
  client.submit.mockRejectedValue(new ApiError('changed', 409, 'VISUAL_RETENTION_REVISION_CONFLICT'))
  render(<ParentVisualRetention childId="a" api={client} />)
  await enable()
  await screen.findByText('设置已被其他操作更新，本次旧版本请求未执行。请核对当前设置后重新选择。')
  expect(localStorage.getItem('hsc-visual-retention:a')).toBeNull()
  expect(client.submit).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('button', { name: '开启自动保留' })).toBeDisabled()
})
it('retains unresolved identity conflicts instead of erasing a possibly committed operation', async () => {
  const client = api()
  client.submit.mockRejectedValue(new ApiError('conflict', 409, 'VISUAL_RETENTION_REQUEST_CONFLICT'))
  render(<ParentVisualRetention childId="a" api={client} />)
  await enable(); await screen.findByRole('alert')
  expect(localStorage.getItem('hsc-visual-retention:a')).not.toBeNull()
  expect(client.get).toHaveBeenCalledTimes(1)
})
it('a late operation cannot populate another child and the original journal remains recoverable', async () => {
  const client = api(); let release!: (r: RetentionResult) => void
  client.submit.mockImplementation(() => new Promise(resolve => { release = resolve }))
  const view = render(<ParentVisualRetention childId="a" api={client} />)
  await enable(); const original = client.submit.mock.calls[0][1]
  client.get.mockResolvedValue({ ...initial, child_id: 'b' })
  view.rerender(<ParentVisualRetention childId="b" api={client} />)
  await act(async () => release(result(original)))
  expect(localStorage.getItem('hsc-visual-retention:a')).not.toBeNull()
  expect(localStorage.getItem('hsc-visual-retention:b')).toBeNull()
  expect(screen.queryByText('学习裁图保留设置已保存。')).toBeNull()
})
it('privacy cleanup drops only the mounted matching child recovery state', async () => {
  localStorage.setItem('hsc-visual-retention:a', JSON.stringify({ version: 1, child_id: 'a', command: { action: 'revoke', expected_revision: 'old', client_action_id: 'old-action' } }))
  const client = api(); render(<ParentVisualRetention childId="a" api={client} />)
  await act(async () => window.dispatchEvent(new CustomEvent('hsc-visual-review-cleared', { detail: 'b' })))
  expect(screen.getByRole('button', { name: '核对原设置操作' })).toBeVisible()
  await act(async () => { localStorage.removeItem('hsc-visual-retention:a'); window.dispatchEvent(new CustomEvent('hsc-visual-review-cleared', { detail: 'a' })) })
  expect(screen.queryByRole('button', { name: '核对原设置操作' })).toBeNull()
})
it('uses the actual API contract and rejects mismatched receipt or cross-child response', async () => {
  const command: RetentionCommand = { action: 'revoke', expected_revision: 'old', client_action_id: 'original' }
  const request = vi.fn().mockResolvedValue(result(command))
  const client = createVisualRetentionApi(request)
  await expect(client.submit('a', command, new AbortController().signal)).resolves.toHaveProperty('receipt.client_action_id', 'original')
  expect(request.mock.calls[0][0]).toBe('/v1/parent/children/a/visual-retention')
  request.mockResolvedValue({ ...result(command), child_id: 'b' })
  await expect(client.get('a', new AbortController().signal)).rejects.toThrow('归属')
  request.mockResolvedValue({ ...result(command), receipt: { ...result(command).receipt, client_action_id: 'wrong' } })
  await expect(client.submit('a', command, new AbortController().signal)).rejects.toThrow('版本')
})
