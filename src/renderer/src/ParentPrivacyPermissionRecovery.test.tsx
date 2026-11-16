import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { webcrypto } from 'node:crypto'
import { readPrivacyIntent } from './privacyOperationRecovery'
import { ApiError, localApi } from './api'
import { PrivacyOperations } from './ParentCenter'
import { curriculumLocalKey, writingAuthorizationLocalKey } from './curriculumSupplyApi'
import { paperVariantAuthorizationKey } from './paperVariantAuthorizationApi'
import type { BackupManifest, PrivacyPreferences, SystemHealth } from './types'

const health: SystemHealth = { database_integrity: 'ok', database_size_bytes: 4096, disk_free_bytes: 100000,
  pending_report_jobs: 0, pending_agent_turns: 0, pending_deletion_jobs: 0, model_failures_last_7_days: 0,
  active_sensor_leases: 0, last_backup: null, maintenance: { daily_backup: null, retention_cleanup: null },
  recent_maintenance_failures: [], raw_media_files_stored: 0, devices: [], device_incidents: [],
  recent_model_failures: [], recent_report_failures: [], recent_agent_failures: [], recent_deletion_failures: [] }
const preferences: PrivacyPreferences = { cloud_model_text_allowed: true, cloud_audio_allowed: false,
  raw_media_storage_allowed: false, model_improvement_allowed: false, model_call_retention_days: 90,
  tutor_turn_retention_days: 180, report_retention_days: 730, updated_at: '2026-09-08T00:00:00Z' }
const backup: BackupManifest = { backup_id: 'backup', path: '/isolated/backup.db', database_version: 47,
  size_bytes: 4096, sha256: 'a'.repeat(64), integrity_status: 'ok', reason: 'manual', created_at: '2026-09-08T00:00:00Z' }
const own = writingAuthorizationLocalKey('child-a', 'question-a'), other = writingAuthorizationLocalKey('child-b', 'question-b')
const journals = [own, other, curriculumLocalKey('child-a'), curriculumLocalKey('child-b'), 'hsc-learning-rubric-review:child-b', 'unrelated-pending']
const paperOwn = paperVariantAuthorizationKey('child-a', 'mistake-a'), paperOther = paperVariantAuthorizationKey('child-b', 'mistake-b')
journals.push(paperOwn, paperOther)
function setup() {
  vi.stubGlobal('crypto', webcrypto)
  vi.spyOn(localApi, 'privacyOperationGeneration').mockResolvedValue({ generation: 'isolated-current-generation' })
  vi.spyOn(localApi, 'privacyOperationReceipt').mockImplementation(async requestId => {
    const intent = readPrivacyIntent()!
    expect(intent.requestId).toBe(requestId)
    const method = intent.kind === 'delete' ? 'deleteChildData' : intent.kind === 'restore' ? 'restoreBackup' : 'restorePortableBackup'
    const call = vi.mocked(localApi[method]).mock.results.at(-1)
    if (!call || call.type !== 'return') throw new Error('No original request')
    await call.value
    return { request_id: requestId, kind: intent.kind, child_id: intent.childId, scope: intent.scope,
      origin_generation: intent.generation, result_generation: 'isolated-current-generation', current_generation: 'isolated-current-generation',
      cleanup_manifest_sha256: intent.manifestSha256, status: 'committed', cleanup_allowed: true,
      reference: { type: intent.kind === 'delete' ? 'deletion_job' : 'audit_event', id: 'isolated-original-reference' },
      local_only: true, cloud_transmission_allowed: false }
  })
  vi.spyOn(localApi, 'recoverNativePrivacyOperation').mockImplementation(requestId => localApi.privacyOperationReceipt(requestId))
  journals.forEach(k => localStorage.setItem(k, 'original pending'))
  vi.spyOn(localApi, 'listBackups').mockResolvedValue([backup])
  vi.spyOn(localApi, 'captureArtifacts').mockResolvedValue([])
  vi.spyOn(localApi, 'systemHealth').mockResolvedValue(health)
  vi.spyOn(localApi, 'deletionPreview').mockResolvedValue({ writing_question_authorizations: 1 })
  window.desktop = { choosePortableBackup: vi.fn().mockResolvedValue({ path: '/isolated/portable.hscbackup' }) } as unknown as NonNullable<typeof window.desktop>
  return render(<PrivacyOperations childId="child-a" initial={preferences} health={health} onHealthChange={vi.fn()} />)
}
async function invoke(operation: string) {
  await screen.findByRole('option', { name: /家长手动/ })
  if (operation === 'local') {
    fireEvent.change(screen.getByLabelText('恢复确认'), { target: { value: '确认恢复' } })
    fireEvent.click(screen.getByRole('button', { name: '恢复' }))
  } else if (operation === 'portable') {
    fireEvent.click(screen.getByRole('button', { name: '选择迁移包' }))
    await screen.findByText('已选择迁移包')
    fireEvent.change(screen.getByLabelText('迁移备份口令'), { target: { value: 'Synthetic isolated passphrase' } })
    fireEvent.change(screen.getByLabelText('迁移恢复确认'), { target: { value: '确认迁移恢复' } })
    fireEvent.click(screen.getByRole('button', { name: '迁移恢复' }))
  } else {
    fireEvent.change(screen.getByLabelText('删除范围'), { target: { value: operation } })
    fireEvent.change(screen.getByLabelText('删除确认'), { target: { value: '确认删除' } })
    fireEvent.click(screen.getByRole('button', { name: '删除' }))
  }
}
describe('Parent privacy permission recovery callbacks', () => {
  afterEach(() => { cleanup(); localStorage.clear(); window.desktop = undefined; vi.restoreAllMocks(); vi.unstubAllGlobals() })
  it.each(['local', 'portable', 'media'])('clears only the appropriate permission journals after successful %s', async operation => {
    const method = operation === 'local' ? 'restoreBackup' : operation === 'portable' ? 'restorePortableBackup' : 'deleteChildData'
    const api = vi.spyOn(localApi, method).mockResolvedValue({ deleted_counts: {}, recovery_rotation: { invalidated_backup_count: 0 } } as never)
    setup(); await invoke(operation)
    await waitFor(() => expect(localStorage.getItem(own)).toBeNull())
    expect(api).toHaveBeenCalledOnce()
    expect(localStorage.getItem(paperOwn)).toBeNull()
    if (operation === 'media') {
      expect(localStorage.getItem(other)).toBe('original pending')
      expect(localStorage.getItem(paperOther)).toBe('original pending')
      expect(localStorage.getItem(curriculumLocalKey('child-a'))).toBe('original pending')
    } else {
      expect(localStorage.getItem(other)).toBeNull()
      expect(localStorage.getItem(paperOther)).toBeNull()
      expect(localStorage.getItem(curriculumLocalKey('child-a'))).toBeNull()
      expect(localStorage.getItem(curriculumLocalKey('child-b'))).toBeNull()
    }
    expect(localStorage.getItem('hsc-learning-rubric-review:child-b')).toBe('original pending')
    expect(localStorage.getItem('unrelated-pending')).toBe('original pending')
  })
  it.each(['local', 'portable', 'media', 'learning', 'knowledge', 'all_history'])('preserves every journal when %s fails before success', async operation => {
    const method = operation === 'local' ? 'restoreBackup' : operation === 'portable' ? 'restorePortableBackup' : 'deleteChildData'
    const api = vi.spyOn(localApi, method).mockRejectedValue(new ApiError('隔离操作尚未完成', 409))
    setup(); await invoke(operation)
    await screen.findByText('隔离操作尚未完成')
    expect(api).toHaveBeenCalledOnce()
    journals.forEach(k => expect(localStorage.getItem(k)).toBe('original pending'))
  })
  it.each(['learning', 'knowledge', 'all_history'])('clears only this child paper permission on %s deletion', async operation => {
    vi.spyOn(localApi, 'deleteChildData').mockResolvedValue({ deleted_counts: {}, recovery_rotation: { invalidated_backup_count: 0 } } as never)
    setup(); await invoke(operation)
    await waitFor(() => expect(localStorage.getItem(paperOwn)).toBeNull())
    expect(localStorage.getItem(paperOther)).toBe('original pending')
    expect(localStorage.getItem('unrelated-pending')).toBe('original pending')
  })
  it.each(['local', 'portable', 'learning', 'media', 'all_history', 'knowledge', 'reports', 'tutor'])(
    'invalidates controlled activity recovery only within successful %s scope', async operation => {
      const method = operation === 'local' ? 'restoreBackup' : operation === 'portable' ? 'restorePortableBackup' : 'deleteChildData'
      vi.spyOn(localApi, method).mockResolvedValue({ deleted_counts: {}, recovery_rotation: { invalidated_backup_count: 0 } } as never)
      const pending = seedControlledEndJournals()
      setup(); await invoke(operation)
      const clearsAll = operation === 'local' || operation === 'portable'
      if (operation === 'local') {
        await waitFor(() => expect((screen.getByRole('button', { name: '恢复' }) as HTMLButtonElement).disabled).toBe(false))
      } else if (operation === 'portable') {
        await waitFor(() => expect((screen.getByLabelText('迁移备份口令') as HTMLInputElement).value).toBe(''))
      } else await screen.findByText(/删除完成：/)
      const clearsOwn = clearsAll || ['learning', 'media', 'all_history'].includes(operation)
      await waitFor(() => {
        for (const item of pending) {
          expect(localStorage.getItem(item.key)).toBe(clearsAll || (clearsOwn && item.childId === 'child-a') ? null : item.raw)
        }
      })
      expect(localStorage.getItem('unrelated-pending')).toBe('original pending')
    }
  )
  it.each(['local', 'portable', 'learning', 'media', 'all_history'])(
    'retains exact pending activity identities when %s fails', async operation => {
      const method = operation === 'local' ? 'restoreBackup' : operation === 'portable' ? 'restorePortableBackup' : 'deleteChildData'
      vi.spyOn(localApi, method).mockRejectedValue(new ApiError('隔离操作尚未完成', 409))
      const pending = seedControlledEndJournals()
      setup(); await invoke(operation)
      await screen.findByText('隔离操作尚未完成')
      for (const item of pending) expect(localStorage.getItem(item.key)).toBe(item.raw)
    }
  )
  it.each(['local', 'portable', 'media'])(
    'retries only local cleanup after a committed %s operation', async operation => {
      const method = operation === 'local' ? 'restoreBackup' : operation === 'portable' ? 'restorePortableBackup' : 'deleteChildData'
      const api = vi.spyOn(localApi, method).mockResolvedValue({ deleted_counts: {}, recovery_rotation: { invalidated_backup_count: 0 } } as never)
      const pending = seedControlledEndJournals()
      const view = setup()
      const remove = Storage.prototype.removeItem
      let unavailable = true
      vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(function (this: Storage, key: string) {
        if (unavailable && key.startsWith('hsc:controlled-end:v1:')) throw new Error('Synthetic storage unavailable')
        return remove.call(this, key)
      })
      await invoke(operation)
      await screen.findByText(operation === 'media' ? /数据已经删除.*本机清理/ : /备份已经恢复.*本机清理/)
      expect(api).toHaveBeenCalledOnce()
      for (const item of pending) expect(localStorage.getItem(item.key)).toBe(item.raw)
      if (operation === 'media') view.rerender(<PrivacyOperations childId="child-b" initial={preferences} health={health} onHealthChange={vi.fn()} />)
      unavailable = false
      const originalId = readPrivacyIntent()!.requestId
      vi.mocked(localApi.recoverNativePrivacyOperation).mockRejectedValueOnce(new Error('Synthetic native receipt unavailable'))
      fireEvent.click(screen.getByRole('button', { name: '重试本机清理' }))
      await screen.findByText('本机资源或原操作尚未核对完成，原请求仍保留。没有再次删除或恢复。')
      for (const item of pending) expect(localStorage.getItem(item.key)).toBe(item.raw)
      expect(readPrivacyIntent()!.requestId).toBe(originalId)
      expect(api).toHaveBeenCalledOnce()
      fireEvent.click(screen.getByRole('button', { name: '重试本机清理' }))
      await waitFor(() => {
        for (const item of pending) expect(localStorage.getItem(item.key)).toBe(operation === 'media' && item.childId !== 'child-a' ? item.raw : null)
      })
      expect(api).toHaveBeenCalledOnce()
      expect(localApi.recoverNativePrivacyOperation).toHaveBeenCalledTimes(2)
      expect(localApi.recoverNativePrivacyOperation).toHaveBeenNthCalledWith(1, originalId)
      expect(localApi.recoverNativePrivacyOperation).toHaveBeenNthCalledWith(2, originalId)
      expect(localStorage.getItem('unrelated-pending')).toBe('original pending')
    }
  )
  it.each(['local', 'media'])('keeps the original %s operation locked while switching children before its response', async operation => {
    const method = operation === 'local' ? 'restoreBackup' : 'deleteChildData'
    let resolve!: (value: never) => void
    const response = new Promise<never>(done => { resolve = done })
    const api = vi.spyOn(localApi, method).mockReturnValue(response)
    const pending = seedControlledEndJournals(), view = setup()
    await invoke(operation)
    await waitFor(() => expect(api).toHaveBeenCalledOnce())
    view.rerender(<PrivacyOperations childId="child-b" initial={preferences} health={health} onHealthChange={vi.fn()} />)
    await invoke(operation)
    await waitFor(() => expect((screen.getByRole('button', { name: operation === 'local' ? '恢复' : '删除' }) as HTMLButtonElement).disabled).toBe(true))
    expect(api).toHaveBeenCalledOnce()
    resolve({ deleted_counts: {}, recovery_rotation: { invalidated_backup_count: 0 } } as never)
    await waitFor(() => {
      for (const item of pending) expect(localStorage.getItem(item.key)).toBe(operation === 'media' && item.childId === 'child-b' ? item.raw : null)
    })
    expect(api).toHaveBeenCalledOnce()
  })
})

function seedControlledEndJournals() {
  return ['child-a', 'child-b'].flatMap(childId => ['video', 'game'].map(kind => {
    const activityId = crypto.randomUUID(), sessionId = `session-${childId}`
    const key = 'hsc:controlled-end:v1:' + JSON.stringify([childId, sessionId, kind, activityId])
    const raw = JSON.stringify({ version: 1, childId, sessionId, kind, activityId,
      requestId: crypto.randomUUID(), reason: 'child_stopped', actualSeconds: 31,
      phase: 'closed', createdAt: '2026-09-09T10:00:00Z', closedAt: '2026-09-09T10:00:31Z' })
    localStorage.setItem(key, raw)
    return { key, raw, childId }
  }))
}
