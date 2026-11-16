import { afterEach, expect, it, vi } from 'vitest'
import { webcrypto } from 'node:crypto'
import { preparePrivacyIntent, readPrivacyIntent, finishPrivacyIntent, applyPrivacyCleanup, operationIdentity, releaseUnappliedPrivacyIntent } from './privacyOperationRecovery'

afterEach(() => { localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
function cryptoReady() { vi.stubGlobal('crypto', webcrypto) }
function receipt(intent: Awaited<ReturnType<typeof preparePrivacyIntent>>, overrides = {}) {
  return { request_id: intent.requestId, kind: intent.kind, child_id: intent.childId, scope: intent.scope,
    origin_generation: intent.generation, result_generation: intent.generation, current_generation: intent.generation,
    cleanup_manifest_sha256: intent.manifestSha256, status: 'committed', cleanup_allowed: true,
    reference: { type: 'deletion_job', id: 'original-job' }, local_only: true, cloud_transmission_allowed: false, ...overrides }
}
it.each(['learning','knowledge','media','tutor','all_history','reports','restore'])('original review cleanup respects exact owner and snapshot for %s',async scope=>{
  cryptoReady()
  const own='hsc-original-source-review:child-a:work',other='hsc-original-source-review:child-b:work',later='hsc-original-source-review:child-a:later'
  localStorage.setItem(own,'old review');localStorage.setItem(other,'other review')
  const notify=vi.fn();window.addEventListener('hsc-original-source-review-cleared',notify)
  try {
    const restoring=scope==='restore'
    const intent=await preparePrivacyIntent(restoring?'restore':'delete',restoring?null:'child-a',restoring?null:scope,'generation')
    localStorage.setItem(later,'later review')
    await applyPrivacyCleanup(intent,receipt(intent,restoring?{reference:{type:'audit_event',id:'restore'}}:{}))
    expect(localStorage.getItem(own)).toBe(scope==='reports'?'old review':null)
    expect(localStorage.getItem(other)).toBe(restoring?null:'other review')
    expect(localStorage.getItem(later)).toBe('later review')
    expect(notify.mock.calls.some(([event])=>event.detail==='child-a')).toBe(scope!=='reports')
  }finally{window.removeEventListener('hsc-original-source-review-cleared',notify)}
})
it.each(['learning','media','knowledge','reports','restore'])('native writing recovery follows exact %s cleanup without deleting a later owner',async scope=>{
  cryptoReady()
  const own='hsc-native-writing-v1:child-a:session-a:prompt-a', other='hsc-native-writing-v1:child-b:session-b:prompt-b'
  localStorage.setItem(own,'original');localStorage.setItem(other,'other-child')
  const restoring=scope==='restore'
  const intent=await preparePrivacyIntent(restoring?'restore':'delete',restoring?null:'child-a',restoring?null:scope,'generation')
  const later='hsc-native-writing-v1:child-a:session-a:prompt-later'
  localStorage.setItem(later,'later request')
  await applyPrivacyCleanup(intent,receipt(intent,restoring?{reference:{type:'audit_event',id:'restore-event'}}:{}))
  expect(localStorage.getItem(own)).toBe(scope==='reports'?'original':null)
  expect(localStorage.getItem(other)).toBe(restoring?null:'other-child')
  expect(localStorage.getItem(later)).toBe('later request')
})

it.each(['learning', 'media', 'knowledge', 'tutor', 'restore'])('cleans only the frozen execution recovery identities for %s', async scope => {
  cryptoReady()
  const own = 'hsc-agent-execution-v1:child-a:session-a:turn-a', other = 'hsc-agent-execution-v1:child-b:session-b:turn-b'
  localStorage.setItem(own, 'old identity'); localStorage.setItem(other, 'other identity')
  const restoring = scope === 'restore'
  const intent = await preparePrivacyIntent(restoring ? 'restore' : 'delete', restoring ? null : 'child-a', restoring ? null : scope, 'generation')
  const later = 'hsc-agent-execution-v1:child-a:session-a:turn-later'
  localStorage.setItem(later, 'new identity')
  await applyPrivacyCleanup(intent, receipt(intent, restoring ? { reference: { type: 'audit_event', id: 'restore-event' } } : {}))
  expect(localStorage.getItem(own)).toBeNull()
  expect(localStorage.getItem(other)).toBe(restoring ? null : 'other identity')
  expect(localStorage.getItem(later)).toBe('new identity')
})

it.each(['media', 'knowledge', 'learning', 'tutor', 'restore'])('clears only frozen visual operation keys for %s and notifies mounted hosts', async scope => {
  cryptoReady()
  const own = `hsc-visual-review:${JSON.stringify(['child-a', 'page', 'revision', 'figure'])}`
  const other = `hsc-visual-review:${JSON.stringify(['child-b', 'page', 'revision', 'figure'])}`
  const notify = vi.fn(); window.addEventListener('hsc-visual-review-cleared', notify)
  for (const key of [own, other, 'hsc-visual-storage:child-a', 'hsc-visual-storage:child-b']) localStorage.setItem(key, 'old-operation')
  const restoring = scope === 'restore'
  const intent = await preparePrivacyIntent(restoring ? 'restore' : 'delete', restoring ? null : 'child-a', restoring ? null : scope, 'storage-generation')
  localStorage.setItem('hsc-visual-storage:child-a', 'later-independent-operation')
  await applyPrivacyCleanup(intent, receipt(intent, restoring ? { reference: { type: 'audit_event', id: 'restore-event' } } : {}))
  expect(localStorage.getItem(own)).toBeNull()
  expect(localStorage.getItem(other)).toBe(restoring ? null : 'old-operation')
  expect(localStorage.getItem('hsc-visual-storage:child-a')).toBe('later-independent-operation')
  expect(localStorage.getItem('hsc-visual-storage:child-b')).toBe(restoring ? null : 'old-operation')
  expect(notify.mock.calls.some(([event]) => event.detail === 'child-a')).toBe(true)
  window.removeEventListener('hsc-visual-review-cleared', notify)
})

it('persists only exact keys and hashes, retaining another child and later writes after remount', async () => {
  cryptoReady()
  const key = 'hsc-writing-authorization:child-a:original', other = 'hsc-writing-authorization:child-b:original'
  localStorage.setItem(key, 'private-original'); localStorage.setItem(other, 'another-child')
  const intent = await preparePrivacyIntent('delete', 'child-a', 'media', 'storage-generation')
  expect(JSON.stringify(intent)).not.toContain('private-original')
  const restarted = readPrivacyIntent()!
  expect(operationIdentity(restarted).request_id).toBe(intent.requestId)
  localStorage.setItem('hsc-writing-authorization:child-a:new', 'new request')
  await applyPrivacyCleanup(restarted, receipt(intent))
  expect(localStorage.getItem(key)).toBeNull()
  expect(localStorage.getItem(other)).toBe('another-child')
  expect(localStorage.getItem('hsc-writing-authorization:child-a:new')).toBe('new request')
  finishPrivacyIntent(restarted)
  expect(readPrivacyIntent()).toBeNull()
})

it('same key changed content survives; storage failure retains original intent for exact retry', async () => {
  cryptoReady()
  const key = 'hsc-writing-authorization:child-a:original'
  localStorage.setItem(key, 'old')
  const intent = await preparePrivacyIntent('delete', 'child-a', 'media', 'storage-generation')
  const remove = vi.spyOn(Storage.prototype, 'removeItem').mockImplementationOnce(() => { throw Error('storage blocked') })
  await expect(applyPrivacyCleanup(intent, receipt(intent))).rejects.toThrow()
  expect(readPrivacyIntent()?.requestId).toBe(intent.requestId)
  remove.mockRestore(); localStorage.setItem(key, 'new independent request')
  await applyPrivacyCleanup(intent, receipt(intent))
  expect(localStorage.getItem(key)).toBe('new independent request')
})

it.each([{ status: 'unknown', cleanup_allowed: false }, { current_generation: 'later-generation' },
  { child_id: 'child-b' }, { request_id: 'other-request' }, { cleanup_manifest_sha256: '0'.repeat(64) }])(
  'cannot clear on mismatched or uncommitted receipt %s', async bad => {
    cryptoReady(); localStorage.setItem('hsc-curriculum-review:child-a', 'original')
    const intent = await preparePrivacyIntent('delete', 'child-a', 'learning', 'storage-generation')
    await expect(applyPrivacyCleanup(intent, receipt(intent, bad))).rejects.toThrow()
    expect(localStorage.getItem('hsc-curriculum-review:child-a')).toBe('original')
  })

it('journal failure means no durable identity may be submitted', async () => {
  cryptoReady(); vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw Error('full') })
  await expect(preparePrivacyIntent('delete', 'child-a', 'media', 'storage-generation')).rejects.toThrow()
})

it.each(['learning', 'knowledge', 'media', 'reports', 'tutor', 'restore'])('retention initialization journal follows exact %s cleanup scope', async scope => {
  cryptoReady()
  const own = 'hsc-visual-retention:child-a', other = 'hsc-visual-retention:child-b'
  localStorage.setItem(own, 'original'); localStorage.setItem(other, 'other')
  const restored = scope === 'restore'
  const intent = await preparePrivacyIntent(restored ? 'restore' : 'delete', restored ? null : 'child-a', restored ? null : scope, 'storage-generation')
  const notified = vi.fn(); window.addEventListener('hsc-visual-review-cleared', notified)
  try {
    await applyPrivacyCleanup(intent, receipt(intent, restored ? { reference: { type: 'audit_event', id: 'restore-retention' } } : {}))
    const cleared = ['learning','knowledge','media','restore'].includes(scope)
    expect(localStorage.getItem(own)).toBe(cleared ? null : 'original')
    expect(localStorage.getItem(other)).toBe(restored ? null : 'other')
    expect(notified.mock.calls.map(c => c[0].detail)).toEqual(restored ? ['child-a','child-b'] : cleared ? ['child-a'] : [])
  } finally { window.removeEventListener('hsc-visual-review-cleared', notified) }
})

it('late original receipt cannot clear or finish a newer intent on the same client', async () => {
  cryptoReady()
  const key = 'hsc-writing-authorization:child-a:original'
  localStorage.setItem(key, 'old')
  const old = await preparePrivacyIntent('delete', 'child-a', 'media', 'storage-generation')
  await applyPrivacyCleanup(old, receipt(old)); finishPrivacyIntent(old)
  localStorage.setItem(key, 'new request')
  const current = await preparePrivacyIntent('delete', 'child-a', 'media', 'storage-generation')
  await expect(applyPrivacyCleanup(old, receipt(old))).rejects.toThrow()
  expect(() => finishPrivacyIntent(old)).toThrow()
  expect(readPrivacyIntent()?.requestId).toBe(current.requestId)
  expect(localStorage.getItem(key)).toBe('new request')
})

it.each(['unknown', 'pending-delete', 'failed', 'superseded'])(
  'parent release of %s never clears records and cannot abandon unknown or original pending jobs', async status => {
    cryptoReady(); const key = 'hsc-writing-authorization:child-a:original'
    localStorage.setItem(key, 'original')
    const intent = await preparePrivacyIntent('delete', 'child-a', 'media', 'storage-generation')
    const view = receipt(intent, { status: status === 'pending-delete' ? 'failed' : status, cleanup_allowed: false,
      reference: status === 'pending-delete' ? { type: 'deletion_job', id: 'pending-original' } : null,
      ...(status === 'superseded' ? { current_generation: 'new-generation' } : {}) })
    if (status === 'unknown' || status === 'pending-delete') expect(() => releaseUnappliedPrivacyIntent(intent, view)).toThrow()
    else { releaseUnappliedPrivacyIntent(intent, view); expect(readPrivacyIntent()).toBeNull() }
    expect(localStorage.getItem(key)).toBe('original')
  })

it.each(['learning', 'media', 'all_history', 'knowledge', 'reports', 'tutor', 'restore'])(
  'freezes exact controlled selection offers for %s without clearing later choices', async scope => {
    cryptoReady()
    const own = 'hsc:controlled-selection:v1:' + JSON.stringify(['child-a', 'session-a'])
    const other = 'hsc:controlled-selection:v1:' + JSON.stringify(['child-b', 'session-b'])
    localStorage.setItem(own, 'private catalog original'); localStorage.setItem(other, 'other catalog')
    const restore = scope === 'restore'
    const intent = await preparePrivacyIntent(restore ? 'restore' : 'delete', restore ? null : 'child-a', restore ? null : scope, 'storage-generation')
    expect(JSON.stringify(intent)).not.toContain('private catalog')
    const newer = 'hsc:controlled-selection:v1:' + JSON.stringify(['child-a', 'new-session'])
    localStorage.setItem(newer, 'later offer')
    await applyPrivacyCleanup(intent, receipt(intent, restore ? { reference: { type: 'audit_event', id: 'restore-audit' } } : {}))
    expect(localStorage.getItem(own)).toBe(['learning', 'media', 'all_history', 'restore'].includes(scope) ? null : 'private catalog original')
    expect(localStorage.getItem(other)).toBe(restore ? null : 'other catalog')
    expect(localStorage.getItem(newer)).toBe('later offer')
  })
