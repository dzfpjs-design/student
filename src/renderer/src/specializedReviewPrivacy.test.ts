import { afterEach, expect, it, vi } from 'vitest'
import { webcrypto } from 'node:crypto'
import { applyPrivacyCleanup, preparePrivacyIntent } from './privacyOperationRecovery'
import { specializedReviewKey, specializedReviewCleared, readSpecializedReviews, saveSpecializedReviews } from './specializedReviewRecovery'
afterEach(() => { localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it.each(['learning', 'knowledge', 'all_history', 'reports', 'restore'])('clears only exact original review storage in privacy scope %s', async scope => {
  vi.stubGlobal('crypto', webcrypto)
  const own = specializedReviewKey('child-a'), other = specializedReviewKey('child-b')
  localStorage.setItem(own, 'private-review-a'); localStorage.setItem(other, 'private-review-b')
  const notify = vi.fn(); window.addEventListener(specializedReviewCleared, notify)
  try {
    const restore = scope === 'restore'
    const intent = await preparePrivacyIntent(restore ? 'restore' : 'delete', restore ? null : 'child-a', restore ? null : scope, 'generation29')
    expect(JSON.stringify(intent)).not.toContain('private-review')
    const result = { request_id: intent.requestId, kind: intent.kind, child_id: intent.childId, scope: intent.scope,
      origin_generation: intent.generation, result_generation: intent.generation, current_generation: intent.generation,
      cleanup_manifest_sha256: intent.manifestSha256, status: 'committed', cleanup_allowed: true,
      reference: { type: restore ? 'audit_event' : 'deletion_job', id: 'original29' }, local_only: true, cloud_transmission_allowed: false }
    await applyPrivacyCleanup(intent, result)
    expect(localStorage.getItem(own)).toBe(scope === 'reports' ? 'private-review-a' : null)
    expect(localStorage.getItem(other)).toBe(restore ? null : 'private-review-b')
    expect(notify.mock.calls.some(([event]) => event.detail === 'child-a')).toBe(scope !== 'reports')
  } finally { window.removeEventListener(specializedReviewCleared, notify) }
})

it('never mutates a journal while privacy is awaiting confirmation', async () => {
  vi.stubGlobal('crypto', webcrypto)
  const before = readSpecializedReviews('child-a')
  await preparePrivacyIntent('delete', 'child-a', 'learning', 'generation29')
  expect(() => saveSpecializedReviews('child-a', before, [])).toThrow()
  expect(localStorage.getItem(specializedReviewKey('child-a'))).toBeNull()
})
