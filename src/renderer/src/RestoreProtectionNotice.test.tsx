import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { webcrypto } from 'node:crypto'
import { localApi } from './api'
import { PrivacyOperations } from './ParentCenter'
import { readPrivacyIntent } from './privacyOperationRecovery'
import type { PrivacyPreferences, SystemHealth } from './types'

const key = 'hsc.restore-protection-notice.v1'
const preferences: PrivacyPreferences = {
  cloud_model_text_allowed: true, cloud_audio_allowed: false, raw_media_storage_allowed: false,
  model_improvement_allowed: false, model_call_retention_days: 90, tutor_turn_retention_days: 180,
  report_retention_days: 730, updated_at: '2026-08-26T08:00:00Z'
}
const health: SystemHealth = {
  database_integrity: 'ok', database_size_bytes: 4096, disk_free_bytes: 100000,
  pending_report_jobs: 0, pending_agent_turns: 0, pending_deletion_jobs: 0,
  model_failures_last_7_days: 0, active_sensor_leases: 0, last_backup: null,
  maintenance: { daily_backup: null, retention_cleanup: null }, recent_maintenance_failures: [],
  raw_media_files_stored: 0, devices: [], device_incidents: [], recent_model_failures: [],
  recent_report_failures: [], recent_agent_failures: [], recent_deletion_failures: []
}
afterEach(() => { cleanup(); localStorage.clear(); sessionStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
function show() {
  return render(<PrivacyOperations childId="child-a" initial={preferences} health={health} onHealthChange={vi.fn()} />)
}
function readOnlyLists() {
  vi.spyOn(localApi, 'listBackups').mockResolvedValue([{ backup_id: 'public-backup', path: '/public/fixture.db', database_version: 86, size_bytes: 4096,
    sha256: 'a'.repeat(64), integrity_status: 'ok', reason: 'manual', created_at: '2026-10-20T00:00:00Z' }])
  vi.spyOn(localApi, 'captureArtifacts').mockResolvedValue([])
}

it('shows the finite parent explanation after the existing reload/remount without exposing a path or ID', async () => {
  readOnlyLists(); sessionStorage.setItem(key, 'internal_database_only')
  const first = show()
  expect(screen.getByRole('status')).toHaveTextContent('没有可用的完整恢复前撤销点')
  first.unmount(); show()
  expect(screen.getByRole('status')).toHaveTextContent('本次使用了内部数据库保护')
  expect(screen.queryByText('/public/fixture.db')).not.toBeInTheDocument()
})

it('ignores unknown stored values rather than rendering storage as diagnostic text', () => {
  readOnlyLists(); sessionStorage.setItem(key, 'UNKNOWN /private/path SECRET')
  show()
  expect(screen.queryByText(/SECRET|内部数据库保护/)).not.toBeInTheDocument()
})

it.each([true, false])('records only the response finite protection fact before the original cleanup (internal=%s)', async internal => {
  readOnlyLists(); vi.stubGlobal('crypto', webcrypto)
  sessionStorage.setItem(key, 'internal_database_only')
  vi.spyOn(localApi, 'privacyOperationGeneration').mockResolvedValue({ generation: '50617198-ed7b-4865-a98f-5b7c1b44532e' })
  const restore = vi.spyOn(localApi, 'restoreBackup').mockResolvedValue({ status: 'restored', integrity_status: 'ok',
    recovery_point_backup_id: internal ? null : 'public-full-undo', ...(internal ? { restore_protection: 'internal_database_only' as const } : {}) })
  // This test does not grant a cleanup receipt or an Electron reload ACK.
  vi.spyOn(localApi, 'privacyOperationReceipt').mockRejectedValue(new Error('CONTROLLED_CLEANUP_PENDING'))
  show()
  await waitFor(() => expect(screen.getByLabelText('选择学习数据备份')).toHaveValue('public-backup'))
  fireEvent.change(screen.getByLabelText('恢复确认'), { target: { value: '确认恢复' } })
  fireEvent.click(screen.getByRole('button', { name: '恢复' }))
  await waitFor(() => expect(restore).toHaveBeenCalledTimes(1))
  await waitFor(() => expect(sessionStorage.getItem(key)).toBe(internal ? 'internal_database_only' : null))
  expect(localStorage.getItem(key)).toBeNull()
})


it.each(['set', 'remove'] as const)('keeps the original committed cleanup and current notice correct when sessionStorage %s throws', async failure => {
  readOnlyLists(); vi.stubGlobal('crypto', webcrypto)
  const internal = failure === 'set'
  if (!internal) sessionStorage.setItem(key, 'internal_database_only')
  localStorage.setItem('unrelated-private-state', 'same-original-value')
  localStorage.setItem('hsc-learning-rubric-review:child-b', 'same-other-child-value')
  const method = failure === 'set' ? 'setItem' : 'removeItem'
  const original = Storage.prototype[method]
  const storage = vi.spyOn(Storage.prototype, method).mockImplementation(function(this: Storage, name: string, value?: string) {
    if (this === sessionStorage && name === key) throw new DOMException('Public controlled storage failure', failure === 'set' ? 'QuotaExceededError' : 'SecurityError')
    if (method === 'setItem') return (original as typeof Storage.prototype.setItem).call(this, name, value!)
    return (original as typeof Storage.prototype.removeItem).call(this, name)
  })
  const generation = '50617198-ed7b-4865-a98f-5b7c1b44532e'
  vi.spyOn(localApi, 'privacyOperationGeneration').mockResolvedValue({ generation })
  const restore = vi.spyOn(localApi, 'restoreBackup').mockResolvedValue({ status: 'restored', integrity_status: 'ok',
    recovery_point_backup_id: internal ? null : 'public-full-undo', ...(internal ? { restore_protection: 'internal_database_only' as const } : {}) })
  const receipt = vi.spyOn(localApi, 'privacyOperationReceipt').mockImplementation(async requestId => {
    const intent = readPrivacyIntent()!
    expect(intent.requestId).toBe(requestId)
    expect(restore).toHaveBeenCalledTimes(1)
    return { request_id: requestId, kind: 'restore', child_id: null, scope: null,
      origin_generation: generation, result_generation: generation, current_generation: generation,
      cleanup_manifest_sha256: intent.manifestSha256, status: 'committed', cleanup_allowed: true,
      reference: { type: 'audit_event', id: 'public-original-restore-audit' },
      local_only: true, cloud_transmission_allowed: false }
  })
  show()
  await waitFor(() => expect(screen.getByLabelText('选择学习数据备份')).toHaveValue('public-backup'))
  fireEvent.change(screen.getByLabelText('恢复确认'), { target: { value: '确认恢复' } })
  fireEvent.click(screen.getByRole('button', { name: '恢复' }))
  await waitFor(() => expect(receipt).toHaveBeenCalledTimes(1))
  await waitFor(() => expect(readPrivacyIntent()).toBeNull())
  expect(restore).toHaveBeenCalledTimes(1)
  expect(storage).toHaveBeenCalled()
  if (internal) expect(screen.getByRole('status')).toHaveTextContent('没有可用的完整恢复前撤销点')
  else expect(screen.queryByText(/本次使用了内部数据库保护/)).not.toBeInTheDocument()
  expect(screen.queryByText(/本机恢复请求尚未保存|原隐私操作的提交状态/)).not.toBeInTheDocument()
  expect(localStorage.getItem('unrelated-private-state')).toBe('same-original-value')
  expect(localStorage.getItem('hsc-learning-rubric-review:child-b')).toBe('same-other-child-value')
})
