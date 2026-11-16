import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { LegacyOcrDispositionPanel } from './LegacyOcrDispositionPanel'
import type { LegacyOcrPreview, LegacyOcrReceipt } from './legacyOcrDispositionTypes'

const key = 'hsc-legacy-ocr-disposition-v1'
const preview: LegacyOcrPreview = { preview_id: 'p', snapshot_fingerprint: 'a'.repeat(64),
  expires_at: '2026-09-07T23:00:00Z', scope: 'legacy_unattributed_ocr_cache', current_database_included: false,
  confirmation_text: '确认清理无法归属的旧OCR缓存',
  counts: { unattributed_jobs: 2, unattributed_cache: 1, protected_jobs: 3, protected_cache: 2 } }
const receipt: LegacyOcrReceipt = { receipt_id: 'r', preview_id: 'p', status: 'completed',
  selected_counts: { unattributed_jobs: 2, unattributed_cache: 1 },
  remaining_counts: { unattributed_jobs: 0, unattributed_cache: 0, protected_jobs: 3, protected_cache: 2 } }
afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks() })
async function prepare() {
  vi.spyOn(localApi, 'legacyOcrPreview').mockResolvedValue(preview)
  const view = render(<LegacyOcrDispositionPanel />)
  fireEvent.click(screen.getByRole('button', { name: '核对旧缓存' }))
  fireEvent.change(await screen.findByLabelText('旧缓存清理确认'), { target: { value: preview.confirmation_text } })
  return view
}

it('persists before submission and retries the same command after remount without automatic writes', async () => {
  const dispose = vi.spyOn(localApi, 'legacyOcrDispose').mockImplementation(async command => {
    expect(JSON.parse(localStorage.getItem(key)!).command).toEqual(command)
    throw new Error('回执未收到')
  })
  const view = await prepare()
  fireEvent.click(screen.getByRole('button', { name: '清理这批未知缓存' }))
  await screen.findByText('回执未收到')
  const command = dispose.mock.calls[0][0]
  view.unmount(); render(<LegacyOcrDispositionPanel />)
  expect(dispose).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('button', { name: '核对旧缓存' })).toBeDisabled()
  dispose.mockResolvedValue(receipt)
  fireEvent.click(screen.getByRole('button', { name: '重试原清理操作' }))
  await screen.findByText(/此前失败的孩子数据删除没有自动重试/)
  expect(screen.getByText(/本次确认范围（历史）/)).toBeInTheDocument()
  expect(screen.queryByText(/^待清理：/)).not.toBeInTheDocument()
  expect(dispose.mock.calls[1][0]).toEqual(command)
  cleanup(); render(<LegacyOcrDispositionPanel />)
  expect(screen.queryByText(/本次确认的未知缓存已清理/)).not.toBeInTheDocument()
  vi.spyOn(localApi, 'legacyOcrReceipt').mockResolvedValue({ ...receipt, restored_invalidated: true })
  fireEvent.click(screen.getByRole('button', { name: '核对上次清理回执' }))
  await screen.findByText(/原回执尚不能证明当前清理完成/)
})

it('does not perform a destructive request if local recovery storage fails', async () => {
  const dispose = vi.spyOn(localApi, 'legacyOcrDispose')
  await prepare()
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('恢复记录无法保存') })
  fireEvent.click(screen.getByRole('button', { name: '清理这批未知缓存' }))
  await screen.findByText('恢复记录无法保存')
  expect(dispose).not.toHaveBeenCalled()
})

it('requires explicit recovery from an unreadable journal without submitting it', async () => {
  localStorage.setItem(key, '{invalid')
  const dispose = vi.spyOn(localApi, 'legacyOcrDispose')
  render(<LegacyOcrDispositionPanel />)
  expect(screen.getByRole('button', { name: '核对旧缓存' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '丢弃损坏的恢复记录，重新核对' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '核对旧缓存' })).toBeEnabled())
  expect(dispose).not.toHaveBeenCalled()
})
