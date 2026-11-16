import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { webcrypto } from 'node:crypto'
import { localApi } from './api'
import { PrivacyOperations } from './ParentCenter'
import { preparePrivacyIntent, readPrivacyIntent } from './privacyOperationRecovery'
import type { PrivacyPreferences, SystemHealth } from './types'

const health = { database_integrity: 'ok', database_size_bytes: 0, disk_free_bytes: 100000,
  pending_report_jobs: 0, pending_agent_turns: 0, pending_deletion_jobs: 0, model_failures_last_7_days: 0,
  active_sensor_leases: 0, last_backup: null, maintenance: { daily_backup: null, retention_cleanup: null },
  recent_maintenance_failures: [], devices: [], device_incidents: [], recent_model_failures: [],
  recent_report_failures: [], recent_agent_failures: [], recent_deletion_failures: [] } as unknown as SystemHealth
const preferences = { cloud_model_text_allowed: false, cloud_audio_allowed: false,
  raw_media_storage_allowed: false, model_improvement_allowed: false,
  model_call_retention_days: 90, tutor_turn_retention_days: 180, report_retention_days: 730,
  updated_at: '2026-09-09T00:00:00Z' } as PrivacyPreferences
const own = 'hsc-writing-authorization:child-a:question', other = 'hsc-writing-authorization:child-b:question'
function mount(child = 'child-a') {
  return render(<PrivacyOperations childId={child} initial={preferences} health={health} onHealthChange={vi.fn()} />)
}
function setup() {
  vi.stubGlobal('crypto', webcrypto)
  vi.spyOn(localApi, 'listBackups').mockResolvedValue([])
  vi.spyOn(localApi, 'captureArtifacts').mockResolvedValue([])
  vi.spyOn(localApi, 'systemHealth').mockResolvedValue(health)
  vi.spyOn(localApi, 'deletionPreview').mockResolvedValue({ evidence: 1 })
  localStorage.setItem(own, 'old original'); localStorage.setItem(other, 'other child')
}
afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('only an explicit retry fences a missing native permit; remount does not retry destruction',async()=>{
  setup()
  const intent=await preparePrivacyIntent('delete','child-a','all_history','original-generation')
  let fenced=false
  const result=()=>({request_id:intent.requestId,kind:'delete',child_id:'child-a',scope:'all_history',
    origin_generation:intent.generation,result_generation:null,current_generation:intent.generation,
    cleanup_manifest_sha256:intent.manifestSha256,status:'failed',cleanup_allowed:false,reference:null,
    local_only:true,cloud_transmission_allowed:false})
  vi.spyOn(localApi,'privacyOperationReceipt').mockImplementation(async()=>{if(!fenced)throw Error('NATIVE_PRIVACY_PENDING');return result()})
  const recovery=vi.spyOn(localApi,'recoverNativePrivacyOperation').mockImplementation(async requestId=>{
    expect(requestId).toBe(intent.requestId);fenced=true;return result()
  })
  const deletion=vi.spyOn(localApi,'deleteChildData')
  mount('child-b')
  const retry=await screen.findByRole('button',{name:'重试本机清理'})
  await waitFor(()=>expect(retry).not.toBeDisabled())
  expect(recovery).not.toHaveBeenCalled()
  fireEvent.click(retry)
  await waitFor(()=>expect(recovery).toHaveBeenCalledOnce())
  const release=await screen.findByRole('button',{name:'保留本机记录，结束本次核对'})
  await waitFor(()=>expect(release).not.toBeDisabled());fireEvent.click(release)
  await waitFor(()=>expect(readPrivacyIntent()).toBeNull())
  expect(deletion).not.toHaveBeenCalled();expect(localStorage.getItem(own)).toBe('old original')
})

it('whole ParentCenter remount exposes the exact durable operation rather than enabling another delete', async () => {
  setup()
  await preparePrivacyIntent('delete', 'child-a', 'media', 'original-generation')
  // No backend permission / lost response must leave the request visible, never a POST.
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(Error('isolated offline')))
  const mutation = vi.spyOn(localApi, 'deleteChildData')
  mount('child-b')
  await screen.findByRole('button', { name: /重试.*(本机清理|原操作)/ })
  expect(screen.getByRole('button', { name: '删除' })).toBeDisabled()
  expect(localStorage.getItem(own)).toBe('old original')
  expect(mutation).not.toHaveBeenCalled()
})

it('lost delete response then remount reads exact receipt, preserves a later request, never rePOSTs', async () => {
  setup()
  const fetcher = vi.fn(async (url: string | URL | Request, options?: RequestInit) => {
    const path = String(url)
    if (path.endsWith('/privacy-operations/generation')) return new Response(JSON.stringify({ generation: 'original-generation' }))
    if (path.includes('/privacy-operations/')) {
      const intent = readPrivacyIntent()!
      expect(path.endsWith(intent.requestId)).toBe(true)
      return new Response(JSON.stringify({ request_id: intent.requestId, kind: 'delete', child_id: 'child-a', scope: 'media',
        origin_generation: 'original-generation', result_generation: 'original-generation', current_generation: 'original-generation',
        cleanup_manifest_sha256: intent.manifestSha256, status: 'committed', cleanup_allowed: true,
        reference: { type: 'deletion_job', id: 'original-job' }, local_only: true, cloud_transmission_allowed: false }))
    }
    throw Error('Unexpected request ' + path + ' ' + options?.method)
  })
  vi.stubGlobal('fetch', fetcher)
  const mutation = vi.spyOn(localApi, 'deleteChildData').mockRejectedValue(Error('response lost after commit'))
  const first = mount()
  fireEvent.change(screen.getByLabelText('删除范围'), { target: { value: 'media' } })
  fireEvent.change(screen.getByLabelText('删除确认'), { target: { value: '确认删除' } })
  // Make receipt read initially offline; only the restarted surface obtains proof.
  const request = fetcher.getMockImplementation()!
  fetcher.mockImplementation(async (url, options) => String(url).endsWith('/generation') ? request(url, options) : Promise.reject(Error('receipt offline')))
  fireEvent.click(screen.getByRole('button', { name: '删除' }))
  await waitFor(() => expect(mutation).toHaveBeenCalledOnce())
  await screen.findByRole('button', { name: /重试/ })
  first.unmount()
  localStorage.setItem('hsc-writing-authorization:child-a:new', 'new request after original operation')
  fetcher.mockImplementation(request)
  mount('child-b')
  await waitFor(() => expect(localStorage.getItem(own)).toBeNull())
  expect(localStorage.getItem(other)).toBe('other child')
  expect(localStorage.getItem('hsc-writing-authorization:child-a:new')).toBe('new request after original operation')
  expect(mutation).toHaveBeenCalledOnce()
  expect(readPrivacyIntent()).toBeNull()
})

it('parent can explicitly finish a failed restore check without clearing records or sending restore again', async () => {
  setup()
  const intent = await preparePrivacyIntent('restore', null, null, 'original-generation')
  const read = vi.spyOn(localApi, 'privacyOperationReceipt').mockResolvedValue({ request_id: intent.requestId,
    kind: 'restore', child_id: null, scope: null, origin_generation: intent.generation,
    result_generation: null, current_generation: 'rollback-generation', cleanup_manifest_sha256: intent.manifestSha256,
    status: 'failed', cleanup_allowed: false, reference: null, local_only: true, cloud_transmission_allowed: false })
  const restore = vi.spyOn(localApi, 'restoreBackup')
  mount()
  const release = await screen.findByRole('button', { name: '保留本机记录，结束本次核对' })
  await waitFor(() => expect(release).not.toBeDisabled())
  fireEvent.click(release)
  await waitFor(() => expect(readPrivacyIntent()).toBeNull())
  expect(localStorage.getItem(own)).toBe('old original')
  expect(localStorage.getItem(other)).toBe('other child')
  expect(read).toHaveBeenCalledTimes(2)
  expect(restore).not.toHaveBeenCalled()
})

it('completed deletion with missing receipt tail offers exact finalization, never another delete', async () => {
  setup()
  const intent = await preparePrivacyIntent('delete', 'child-a', 'media', 'original-generation')
  let finalized = false
  const view = () => ({ request_id: intent.requestId, kind: 'delete', child_id: 'child-a', scope: 'media',
    origin_generation: intent.generation, result_generation: finalized ? intent.generation : null,
    current_generation: intent.generation, cleanup_manifest_sha256: intent.manifestSha256,
    status: finalized ? 'committed' : 'finalization_required', cleanup_allowed: finalized,
    reference: { type: 'deletion_job', id: 'original-completed-job' }, local_only: true, cloud_transmission_allowed: false })
  vi.spyOn(localApi, 'privacyOperationReceipt').mockImplementation(async () => view())
  const finalize = vi.spyOn(localApi, 'finalizePrivacyOperation').mockImplementation(async id => {
    expect(id).toBe(intent.requestId); finalized = true
    throw Error('Finalization response lost after metadata commit')
  })
  const deletion = vi.spyOn(localApi, 'deleteChildData'), retry = vi.spyOn(localApi, 'retryDeletion')
  mount('child-b')
  const button = await screen.findByRole('button', { name: '核验原删除收尾' })
  await waitFor(() => expect(button).not.toBeDisabled())
  fireEvent.click(button)
  await screen.findByText('原删除收尾尚未核对完成。没有再次删除数据，请重试核验原操作。')
  expect(localStorage.getItem(own)).toBe('old original')
  fireEvent.click(button)
  await waitFor(() => expect(localStorage.getItem(own)).toBeNull())
  expect(localStorage.getItem(other)).toBe('other child')
  expect(finalize).toHaveBeenCalledOnce()
  expect(deletion).not.toHaveBeenCalled(); expect(retry).not.toHaveBeenCalled()
  expect(readPrivacyIntent()).toBeNull()
})
