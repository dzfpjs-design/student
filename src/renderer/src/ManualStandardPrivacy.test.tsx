import { afterEach, expect, it, vi } from 'vitest'
import { webcrypto } from 'node:crypto'
import { preparePrivacyIntent, applyPrivacyCleanup } from './privacyOperationRecovery'
afterEach(() => { localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
it.each(['learning', 'knowledge', 'all_history', 'media', 'restore', 'portable_restore'])('manual work follows exact %s cleanup and preserves later writes', async scope => {
  vi.stubGlobal('crypto', webcrypto)
  const own = 'hsc-manual-standard:child-a', other = 'hsc-manual-standard:child-b'
  localStorage.setItem(own, 'original parent work'); localStorage.setItem(other, 'other child work')
  const restoring = ['restore', 'portable_restore'].includes(scope)
  const intent = await preparePrivacyIntent(restoring ? scope as 'restore' | 'portable_restore' : 'delete', restoring ? null : 'child-a', restoring ? null : scope, 'generation')
  const notify = vi.fn(); window.addEventListener('hsc-manual-standard-cleared', notify)
  try {
    await applyPrivacyCleanup(intent, { request_id: intent.requestId, kind: intent.kind, child_id: intent.childId, scope: intent.scope,
      origin_generation: intent.generation, result_generation: intent.generation, current_generation: intent.generation,
      cleanup_manifest_sha256: intent.manifestSha256, status: 'committed', cleanup_allowed: true,
      reference: { type: restoring ? 'audit_event' : 'deletion_job', id: 'original-job' }, local_only: true, cloud_transmission_allowed: false })
    expect(localStorage.getItem(own)).toBe(scope === 'media' ? 'original parent work' : null)
    expect(localStorage.getItem(other)).toBe(restoring ? null : 'other child work')
    expect(notify.mock.calls.some(([event]) => event.detail === 'child-a')).toBe(scope !== 'media')
  } finally { window.removeEventListener('hsc-manual-standard-cleared', notify) }
})

it('does not erase a newer manual draft written after the frozen restore manifest', async () => {
  vi.stubGlobal('crypto', webcrypto)
  const key = 'hsc-manual-standard:child-a'; localStorage.setItem(key, 'old')
  const intent = await preparePrivacyIntent('restore', null, null, 'generation')
  localStorage.setItem(key, 'later work')
  await applyPrivacyCleanup(intent, { request_id: intent.requestId, kind: intent.kind, child_id: intent.childId, scope: intent.scope,
    origin_generation: intent.generation, result_generation: intent.generation, current_generation: intent.generation,
    cleanup_manifest_sha256: intent.manifestSha256, status: 'committed', cleanup_allowed: true,
    reference: { type: 'audit_event', id: 'restore-job' }, local_only: true, cloud_transmission_allowed: false })
  expect(localStorage.getItem(key)).toBe('later work')
})
