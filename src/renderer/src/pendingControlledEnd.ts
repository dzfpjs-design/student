import { clearControlledSelections } from './controlledSelection'

export type ControlledEndScope = {
  childId: string
  sessionId: string
  kind: 'video' | 'game'
  activityId: string
}
export type PendingControlledEnd = ControlledEndScope & {
  version: 1
  requestId: string
  reason: string
  actualSeconds: number
  phase: 'closure_required' | 'closed'
  createdAt: string
  closedAt?: string
}
export type ControlledEndReceipt = {
  child_id: string
  session_id: string
  content_session_id?: string
  game_session_id?: string
  status: string
  device_closed: true
  replayed?: boolean
  task_resumed: false
}

const prefix = 'hsc:controlled-end:v1:'
const invalidated = new Set<string>()
export class PendingControlledEndError extends Error {}
const failure = (): PendingControlledEndError => new PendingControlledEndError('结束记录尚未可靠保存或核对。原活动仍待确认，请重试停止原活动；没有操作其他活动。')
const text = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 256
function scopeKey(scope: ControlledEndScope): string {
  if (!text(scope.childId) || !text(scope.sessionId) || !text(scope.activityId)
    || !['video', 'game'].includes(scope.kind)) throw failure()
  return prefix + JSON.stringify([scope.childId, scope.sessionId, scope.kind, scope.activityId])
}
function parse(raw: string, key: string): PendingControlledEnd {
  const value = JSON.parse(raw) as PendingControlledEnd
  if (!value || typeof value !== 'object' || Array.isArray(value) || value.version !== 1
    || scopeKey(value) !== key || !text(value.requestId) || !text(value.reason)
    || !Number.isSafeInteger(value.actualSeconds) || value.actualSeconds < 0
    || !['closure_required', 'closed'].includes(value.phase) || !Number.isFinite(Date.parse(value.createdAt))
    || (value.phase === 'closed' && !Number.isFinite(Date.parse(value.closedAt ?? '')))) throw failure()
  return value
}
function read(scope: ControlledEndScope): PendingControlledEnd | null {
  try {
    const key = scopeKey(scope), raw = localStorage.getItem(key)
    return raw === null ? null : parse(raw, key)
  } catch { throw failure() }
}
function save(value: PendingControlledEnd): PendingControlledEnd {
  try {
    const key = scopeKey(value), raw = JSON.stringify(value)
    localStorage.setItem(key, raw)
    if (localStorage.getItem(key) !== raw) throw failure()
    return value
  } catch { throw failure() }
}
function current(expected: PendingControlledEnd): PendingControlledEnd {
  if (invalidated.has(scopeKey(expected))) throw failure()
  const value = read(expected)
  if (!value || value.requestId !== expected.requestId || value.reason !== expected.reason
    || value.actualSeconds !== expected.actualSeconds || value.createdAt !== expected.createdAt) throw failure()
  return value
}

export function preparePendingControlledEnd(scope: ControlledEndScope,
  request: { reason: string; actualSeconds: number }): PendingControlledEnd {
  if (invalidated.has(scopeKey(scope))) throw failure()
  const existing = read(scope)
  if (existing) return existing
  if (!text(request.reason) || !Number.isSafeInteger(request.actualSeconds) || request.actualSeconds < 0) throw failure()
  return save({version: 1, childId: scope.childId, sessionId: scope.sessionId, kind: scope.kind,
    activityId: scope.activityId, requestId: crypto.randomUUID(), reason: request.reason,
    actualSeconds: request.actualSeconds, phase: 'closure_required', createdAt: new Date().toISOString()})
}

export function readPendingControlledEnds(sessionId: string, childId: string,
  kind: 'video' | 'game'): PendingControlledEnd[] {
  try {
    const scopedPrefix = prefix + JSON.stringify([childId, sessionId, kind]).slice(0, -1) + ','
    const values: PendingControlledEnd[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (!key?.startsWith(scopedPrefix)) continue
      const raw = localStorage.getItem(key)
      if (raw !== null) values.push(parse(raw, key))
    }
    return values
  } catch { throw failure() }
}

export function markControlledEndClosed(expected: PendingControlledEnd,
  receipt: { closed: boolean; sessionId?: string }): PendingControlledEnd {
  if (receipt.closed !== true || receipt.sessionId !== expected.activityId) throw failure()
  const value = current(expected)
  return value.phase === 'closed' ? value : save({...value, phase: 'closed', closedAt: new Date().toISOString()})
}

export function finishPendingControlledEnd(expected: PendingControlledEnd,
  receipt: ControlledEndReceipt): void {
  const value = current(expected)
  if (value.phase !== 'closed' || receipt.child_id !== value.childId || receipt.session_id !== value.sessionId
    || receipt[value.kind === 'video' ? 'content_session_id' : 'game_session_id'] !== value.activityId
    || receipt.device_closed !== true || receipt.task_resumed !== false
    || !['completed', 'stopped', 'expired', 'interrupted', 'failed'].includes(receipt.status)) throw failure()
  try {
    const key = scopeKey(value)
    localStorage.removeItem(key)
    if (localStorage.getItem(key) !== null) throw failure()
  } catch { throw failure() }
}

// Called only after explicit deletion/restore succeeds. Prevent still-mounted callbacks from re-creating removed requests.
export function clearPendingControlledEnds(childId?: string, sessionId?: string): void {
  clearControlledSelections(childId, sessionId)
  const keys: string[] = []
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (!key?.startsWith(prefix)) continue
      const scope = JSON.parse(key.slice(prefix.length)) as unknown
      if (!Array.isArray(scope) || scope.length !== 4) throw failure()
      if (childId !== undefined && scope[0] !== childId) continue
      if (sessionId !== undefined && scope[1] !== sessionId) continue
      keys.push(key)
    }
    for (const key of keys) {
      invalidated.add(key)
      localStorage.removeItem(key)
      if (localStorage.getItem(key) !== null) throw failure()
    }
  } catch { throw failure() }
}

/** Exact privacy cleanup must invalidate the old callback without removing a later request. */
export function discardExactPendingControlledEnd(key: string, expected: string): void {
  if (!key.startsWith(prefix) || localStorage.getItem(key) !== expected) throw failure()
  invalidated.add(key)
  localStorage.removeItem(key)
  if (localStorage.getItem(key) === expected) throw failure()
}

// A remount can join an outstanding request, but no process-local state replaces the durable journal.
const inFlight = new Map<string, Promise<ControlledEndReceipt>>()
export function runPendingControlledEnd(value: PendingControlledEnd,
  run: (pending: PendingControlledEnd) => Promise<ControlledEndReceipt>): Promise<ControlledEndReceipt> {
  const key = scopeKey(value)
  const prior = inFlight.get(key)
  if (prior) return prior
  const result = Promise.resolve().then(() => run(current(value))).finally(() => {
    if (inFlight.get(key) === result) inFlight.delete(key)
  })
  inFlight.set(key, result)
  return result
}
