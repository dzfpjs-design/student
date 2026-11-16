import { discardExactPendingControlledEnd } from './pendingControlledEnd'

export type PrivacyOperationIdentity = { request_id: string; expected_generation: string; cleanup_manifest_sha256: string }
export type PrivacyOperationReceipt = {
  request_id: string; kind: string; child_id: string | null; scope: string | null
  origin_generation: string; result_generation: string | null; current_generation: string
  cleanup_manifest_sha256: string; status: string; cleanup_allowed: boolean
  reference: { type: string; id: string } | null; local_only: boolean; cloud_transmission_allowed: boolean
}
type Kind = 'delete' | 'restore' | 'portable_restore'
type Entry = { key: string; sha256: string }
export type PrivacyIntent = {
  version: 1; requestId: string; kind: Kind; childId: string | null; scope: string | null
  generation: string; entries: Entry[]; manifestSha256: string
}
const journal = 'hsc:privacy-operation-intent:v1'
const hashPattern = /^[a-f0-9]{64}$/
const scopes = ['learning', 'knowledge', 'media', 'tutor', 'reports', 'all_history']
const failure = () => new Error('原隐私操作或本机恢复记录尚未核对完成，未重复恢复或删除。请重试核对原操作。')
async function sha(value: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value))
  return Array.from(new Uint8Array(digest), n => n.toString(16).padStart(2, '0')).join('')
}

function notifyRemoved(key: string): void {
  const notify = (name: string, detail: unknown) => window.dispatchEvent(new CustomEvent(name, { detail }))
  const childAfter = (prefix: string, compound = false) => decodeURIComponent(key.slice(prefix.length).split(compound ? ':' : '\0')[0])
  for (const prefix of ['hsc-cloud-mistake-draft-v1:', 'hsc-cloud-mistake-pending-v1:', 'hsc-cloud-mistake-intake-v1:']) {
    if (key.startsWith(prefix)) notify('hsc-cloud-mistake-cleared', {childId: childAfter(prefix, true)})
  }
  if (key.startsWith('hsc:behavior-goal-review:')) notify('hsc-behavior-goal-recovery-cleared', { childId: childAfter('hsc:behavior-goal-review:', true) })
  if (key.startsWith('hsc-original-source-review:')) notify('hsc-original-source-review-cleared', childAfter('hsc-original-source-review:', true))
  if (key.startsWith('hsc-paper-variant-authorization:')) notify('hsc-paper-variant-authorization-cleared', childAfter('hsc-paper-variant-authorization:', true))
  if (key.startsWith('hsc:source-revision:')) notify('hsc:source-revision-cleared', { childId: childAfter('hsc:source-revision:') })
  if (key.startsWith('hsc-writing-authorization:')) {
    const [child, item] = key.slice('hsc-writing-authorization:'.length).split(':').map(decodeURIComponent)
    notify('hsc-writing-authorization-cleared', { child, item })
  }
  if (key.startsWith('hsc-curriculum-review:')) notify('hsc-curriculum-review-cleared', childAfter('hsc-curriculum-review:'))
  if (key.startsWith('hsc-manual-standard:')) notify('hsc-manual-standard-cleared', childAfter('hsc-manual-standard:'))
  if (key.startsWith('hsc-specialized-review:')) notify('hsc-specialized-review-cleared', childAfter('hsc-specialized-review:'))
  if (key.startsWith('hsc-curriculum-oral-author:')) notify('hsc-curriculum-review-cleared', childAfter('hsc-curriculum-oral-author:'))
  if (key.startsWith('hsc:ocr-review-recovery:')) notify('hsc:ocr-review-recovery-cleared', childAfter('hsc:ocr-review-recovery:'))
  if (key.startsWith('hsc-learning-rubric-review:')) notify('hsc-learning-rubric-review-cleared', childAfter('hsc-learning-rubric-review:'))
  if (key.startsWith('hsc-visual-storage:')) notify('hsc-visual-review-cleared', key.slice('hsc-visual-storage:'.length))
  if (key.startsWith('hsc-visual-retention:')) notify('hsc-visual-review-cleared', key.slice('hsc-visual-retention:'.length))
  if (key.startsWith('hsc-visual-review:')) notify('hsc-visual-review-cleared', JSON.parse(key.slice('hsc-visual-review:'.length))[0])
  if (key.startsWith('hsc-tutor-playback-pending-v1:')) notify('hsc-tutor-playback-forget', { childId: childAfter('hsc-tutor-playback-pending-v1:', true) })
}

// This is the existing explicit-delete policy, evaluated once before dispatch.
// Recovery never enumerates prefixes again; it consumes the frozen exact keys.
function allowed(key: string, kind: Kind, child: string | null, scope: string | null): boolean {
  const all = kind !== 'delete', has = (...values: string[]) => all || scope === 'all_history' || values.includes(scope!)
  const prefix = (base: string, encoded = true) => key.startsWith(base + (all ? '' : `${encoded ? encodeURIComponent(child!) : child}:`))
  const exact = (base: string, encoded = false) => all ? key.startsWith(base) : key === base + (encoded ? encodeURIComponent(child!) : child)
  if (has('learning', 'knowledge', 'tutor') && (prefix('hsc-cloud-mistake-draft-v1:') || prefix('hsc-cloud-mistake-pending-v1:') || exact('hsc-cloud-mistake-intake-v1:', true))) return true
  if (has('learning', 'knowledge', 'media', 'tutor') && prefix('hsc-agent-execution-v1:', false)) return true
  if (has('learning', 'knowledge', 'media', 'tutor') && prefix('hsc-original-source-review:')) return true
  if (has('learning', 'knowledge', 'media') && prefix('hsc-native-writing-v1:')) return true
  if (has('learning', 'knowledge', 'media') && (prefix('hsc:behavior-goal-review:') || prefix('hsc-paper-variant-authorization:'))) return true
  if (has('knowledge') && exact('hsc:source-revision:')) return true
  if (has('learning') && prefix('hsc:goal-rule-confirmation:')) return true
  if (has('learning', 'knowledge', 'reports') && exact('hsc:period-evidence-pending:', true)) return true
  if (has('learning', 'knowledge') && (exact('hsc-curriculum-review:') || exact('hsc-curriculum-oral-author:') || exact('hsc-manual-standard:'))) return true
  if (has('learning', 'knowledge') && exact('hsc-specialized-review:')) return true
  if (has('learning', 'knowledge', 'media') && prefix('hsc-writing-authorization:')) return true
  if (has('learning', 'media') && exact('hsc:ocr-review-recovery:')) return true
  if (has('learning', 'knowledge', 'media', 'tutor') && exact('hsc-visual-storage:')) return true
  if (has('learning', 'knowledge', 'media') && exact('hsc-visual-retention:')) return true
  if (has('learning', 'knowledge', 'media', 'tutor') && key.startsWith('hsc-visual-review:')) {
    const tuple: unknown = JSON.parse(key.slice('hsc-visual-review:'.length))
    if (!Array.isArray(tuple) || tuple.length !== 4 || tuple.some(v => typeof v !== 'string' || !v)) throw failure()
    return all || tuple[0] === child
  }
  if (has('learning', 'media') && key.startsWith('hsc:controlled-end:v1:')) {
    const tuple: unknown = JSON.parse(key.slice('hsc:controlled-end:v1:'.length))
    if (!Array.isArray(tuple) || tuple.length !== 4 || tuple.some(v => typeof v !== 'string' || !v)
      || !['game', 'video'].includes(tuple[2])) throw failure()
    return all || tuple[0] === child
  }
  if (has('learning', 'media') && key.startsWith('hsc:controlled-selection:v1:')) {
    const tuple: unknown = JSON.parse(key.slice('hsc:controlled-selection:v1:'.length))
    if (!Array.isArray(tuple) || tuple.length !== 2 || tuple.some(v => typeof v !== 'string' || !v)) throw failure()
    return all || tuple[0] === child
  }
  // Preserve the older restore scope: these journals are not cleared by restore.
  if (!all && has('reports') && prefix('hsc-parent-action-pending:', false)) return true
  if (!all && has('tutor') && prefix('hsc-tutor-playback-pending-v1:')) return true
  if (!all && has('learning', 'tutor') && exact('hsc-learning-rubric-review:')) return true
  if (!all && has('learning') && prefix('hsc-parent-pet-material:')) return true
  return false
}

export function readPrivacyIntent(): PrivacyIntent | null {
  const raw = localStorage.getItem(journal)
  if (raw === null) return null
  const value = JSON.parse(raw) as PrivacyIntent
  if (!value || value.version !== 1 || typeof value.requestId !== 'string' || value.requestId.length < 8
    || !['delete', 'restore', 'portable_restore'].includes(value.kind) || typeof value.generation !== 'string'
    || value.generation.length < 8 || !hashPattern.test(value.manifestSha256)
    || (value.kind === 'delete' ? typeof value.childId !== 'string' || !value.childId || !scopes.includes(value.scope!)
      : value.childId !== null || value.scope !== null)
    || !Array.isArray(value.entries) || value.entries.length > 10000
    || new Set(value.entries.map(v => v?.key)).size !== value.entries.length
    || value.entries.some(e => !e || typeof e.key !== 'string' || !hashPattern.test(e.sha256)
      || !allowed(e.key, value.kind, value.childId, value.scope))) throw failure()
  return value
}

export function hasPrivacyIntent(): boolean {
  try { return readPrivacyIntent() !== null } catch { return true }
}

export function canReleasePrivacyIntent(value: PrivacyIntent, receipt: PrivacyOperationReceipt): boolean {
  return JSON.stringify(readPrivacyIntent()) === JSON.stringify(value)
    && receipt.request_id === value.requestId && receipt.kind === value.kind
    && receipt.child_id === value.childId && receipt.scope === value.scope
    && receipt.origin_generation === value.generation && receipt.cleanup_manifest_sha256 === value.manifestSha256
    && receipt.cleanup_allowed === false && receipt.local_only === true && receipt.cloud_transmission_allowed === false
    && ((receipt.status === 'failed' && receipt.reference === null)
      || (receipt.status === 'superseded' && !!receipt.result_generation && receipt.result_generation !== receipt.current_generation))
}

export function canFinalizePrivacyIntent(value: PrivacyIntent, receipt: PrivacyOperationReceipt): boolean {
  return JSON.stringify(readPrivacyIntent()) === JSON.stringify(value)
    && value.kind === 'delete' && receipt.kind === value.kind && receipt.request_id === value.requestId
    && receipt.child_id === value.childId && receipt.scope === value.scope
    && receipt.origin_generation === value.generation && receipt.current_generation === value.generation
    && receipt.cleanup_manifest_sha256 === value.manifestSha256 && receipt.cleanup_allowed === false
    && receipt.status === 'finalization_required' && receipt.reference?.type === 'deletion_job' && !!receipt.reference.id
    && receipt.local_only === true && receipt.cloud_transmission_allowed === false
}

export function releaseUnappliedPrivacyIntent(value: PrivacyIntent, receipt: PrivacyOperationReceipt): void {
  if (!canReleasePrivacyIntent(value, receipt)) throw failure()
  // An explicit parent acknowledgement only releases the intent, never source journals.
  finishPrivacyIntent(value)
}

export function operationIdentity(value: PrivacyIntent): PrivacyOperationIdentity {
  return { request_id: value.requestId, expected_generation: value.generation, cleanup_manifest_sha256: value.manifestSha256 }
}

export async function preparePrivacyIntent(kind: Kind, childId: string | null, scope: string | null, generation: string): Promise<PrivacyIntent> {
  if (readPrivacyIntent()) throw failure()
  const entries: Entry[] = []
  for (const key of Object.keys(localStorage).sort()) {
    if (!allowed(key, kind, childId, scope)) continue
    const raw = localStorage.getItem(key)
    if (raw === null) continue
    const sha256 = await sha(raw)
    if (localStorage.getItem(key) !== raw) throw failure()
    entries.push({ key, sha256 })
  }
  const value: PrivacyIntent = { version: 1, requestId: crypto.randomUUID(), kind, childId, scope, generation,
    entries, manifestSha256: await sha(JSON.stringify(entries)) }
  if (readPrivacyIntent()) throw failure()
  const raw = JSON.stringify(value)
  localStorage.setItem(journal, raw)
  if (localStorage.getItem(journal) !== raw || !readPrivacyIntent()) throw failure()
  return value
}

async function verifyReceipt(value: PrivacyIntent, receipt: PrivacyOperationReceipt): Promise<void> {
  if (JSON.stringify(readPrivacyIntent()) !== JSON.stringify(value)
    || await sha(JSON.stringify(value.entries)) !== value.manifestSha256
    || receipt.request_id !== value.requestId || receipt.kind !== value.kind
    || receipt.child_id !== value.childId || receipt.scope !== value.scope
    || receipt.origin_generation !== value.generation || receipt.cleanup_manifest_sha256 !== value.manifestSha256
    || receipt.status !== 'committed' || receipt.cleanup_allowed !== true || !receipt.result_generation
    || receipt.result_generation !== receipt.current_generation || receipt.local_only !== true
    || receipt.cloud_transmission_allowed !== false || !receipt.reference?.id
    || receipt.reference.type !== (value.kind === 'delete' ? 'deletion_job' : 'audit_event')) throw failure()
}

export async function applyPrivacyCleanup(value: PrivacyIntent, receipt: PrivacyOperationReceipt): Promise<void> {
  await verifyReceipt(value, receipt)
  for (const entry of value.entries) {
    const raw = localStorage.getItem(entry.key)
    if (raw === null) continue
    const currentHash = await sha(raw)
    if (JSON.stringify(readPrivacyIntent()) !== JSON.stringify(value)) throw failure()
    // No await between final value comparison and exact removal.
    if (currentHash !== entry.sha256 || localStorage.getItem(entry.key) !== raw) continue
    if (entry.key.startsWith('hsc:controlled-end:v1:')) discardExactPendingControlledEnd(entry.key, raw)
    else localStorage.removeItem(entry.key)
    if (localStorage.getItem(entry.key) === raw) throw failure()
    notifyRemoved(entry.key)
  }
}

export function finishPrivacyIntent(value: PrivacyIntent): void {
  if (JSON.stringify(readPrivacyIntent()) !== JSON.stringify(value)) throw failure()
  localStorage.removeItem(journal)
  if (localStorage.getItem(journal) !== null) throw failure()
}

const recovering = new Map<string, Promise<void>>()
export class PrivacyCleanupPendingError extends Error {}
export function recoverPrivacyCleanup(value: PrivacyIntent, read: (id: string) => Promise<PrivacyOperationReceipt>,
  tail: () => Promise<void>): Promise<void> {
  const joined = recovering.get(value.requestId)
  if (joined) return joined
  const run = (async () => {
    const receipt = await read(value.requestId)
    await verifyReceipt(value, receipt)
    try {
      await applyPrivacyCleanup(value, receipt)
      await tail()
      finishPrivacyIntent(value)
    } catch { throw new PrivacyCleanupPendingError('Original commit verified; local cleanup incomplete') }
  })().finally(() => { if (recovering.get(value.requestId) === run) recovering.delete(value.requestId) })
  recovering.set(value.requestId, run)
  return run
}
