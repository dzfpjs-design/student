import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { carryoverConfirmationAtArrival, finishCarryoverReview, invalidateCarryoverReview,
  markCarryoverReadback, prepareCarryoverReview, readCarryoverReview, refreshCarryoverReadback } from './pendingCarryoverReview'
import type { DeferredTaskCandidate } from './types'

const scope = { sessionId: 'storage-session', childId: 'storage-child' }
const key = 'hsc-pending-carryover-v1:storage-session'
const source = { source_plan_item_id: 'original', source_version: 'a'.repeat(64), title: '原作业', subject: '数学' } as DeferredTaskCandidate
const changed = { ...source, source_version: 'b'.repeat(64) }
const prepare = () => prepareCarryoverReview(scope, source, 'continue_today', '孩子确认', 'original-turn')
const details = (request: ReturnType<typeof prepare>) => ({ not_applied: true, session_id: scope.sessionId,
  child_id: scope.childId, client_action_id: request.requestId, candidate: changed })
const receipt = (request: ReturnType<typeof prepare>) => ({ review_id: 'original-review', source_plan_item_id: request.sourcePlanItemId,
  source_session_id: 'yesterday', child_id: request.childId, target_session_id: request.sessionId,
  decision: request.decision, reason: request.reason, target_homework_id: 'actual-homework', reviewed_at: '2026-09-10' })

beforeEach(() => localStorage.clear())
afterEach(() => { vi.restoreAllMocks(); localStorage.clear() })

it.each(['getItem', 'setItem'] as const)('unavailable initial %s never returns a dispatchable request', method => {
  vi.spyOn(Storage.prototype, method).mockImplementation(() => { throw new DOMException('storage unavailable', 'SecurityError') })
  expect(prepare).toThrow('storage unavailable')
  expect(carryoverConfirmationAtArrival(scope.sessionId, scope.childId)).toBeNull()
})

it.each([1, 2])('invalidation write %s failure retains the exact original request for retry', failedWrite => {
  const original = prepare()
  const set = Storage.prototype.setItem
  let calls = 0
  const failure = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, k, v) {
    if (++calls === failedWrite) throw new DOMException('quota full', 'QuotaExceededError')
    set.call(this, k, v)
  })
  expect(() => invalidateCarryoverReview(original, details(original))).toThrow('quota full')
  failure.mockRestore()
  expect(readCarryoverReview(scope.sessionId, scope.childId)).toEqual(original)
  expect(prepareCarryoverReview(scope, { ...source, source_plan_item_id: 'different-first-task' },
    'continue_today', 'new intent', 'new-turn')).toEqual(original)
  invalidateCarryoverReview(original, details(original))
  expect(readCarryoverReview(scope.sessionId, scope.childId)).toMatchObject({ sourceVersion: changed.source_version, readbackReady: false })
})

it('failed playback-end persistence grants no confirmation and can be retried without changing the task', () => {
  const original = prepare(); invalidateCarryoverReview(original, details(original))
  const pending = readCarryoverReview(scope.sessionId, scope.childId)!
  const failure = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('disk unavailable') })
  expect(() => markCarryoverReadback(pending)).toThrow('disk unavailable')
  expect(carryoverConfirmationAtArrival(scope.sessionId, scope.childId)).toBeNull()
  failure.mockRestore()
  markCarryoverReadback(pending)
  expect(carryoverConfirmationAtArrival(scope.sessionId, scope.childId)).toBe(pending.confirmationId)
})

it('failed new-confirmation save leaves the prior readback, not an unsaved new command', () => {
  const original = prepare(); invalidateCarryoverReview(original, details(original))
  const pending = readCarryoverReview(scope.sessionId, scope.childId)!
  markCarryoverReadback(pending)
  const before = readCarryoverReview(scope.sessionId, scope.childId)
  const failure = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('disk unavailable') })
  expect(() => prepareCarryoverReview(scope, changed, 'continue_today', '孩子确认', 'fresh-turn', pending.confirmationId)).toThrow()
  failure.mockRestore()
  expect(readCarryoverReview(scope.sessionId, scope.childId)).toEqual(before)
})

it('failed receipt removal retains original key even when the next candidate has changed', () => {
  const original = prepare()
  const failure = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('disk unavailable') })
  expect(() => finishCarryoverReview(original, receipt(original))).toThrow('disk unavailable')
  expect(prepareCarryoverReview(scope, undefined, 'continue_today', 'later words', 'new-turn')).toEqual(original)
  failure.mockRestore()
  finishCarryoverReview(original, receipt(original))
  expect(readCarryoverReview(scope.sessionId, scope.childId)).toBeNull()
  // Exact success can be observed twice, but does not create or remove another request.
  finishCarryoverReview(original, receipt(original))
})

it('old receipt cannot clear a newer request after confirmation, even for the same source ID', () => {
  const original = prepare(); invalidateCarryoverReview(original, details(original))
  const pending = readCarryoverReview(scope.sessionId, scope.childId)!
  markCarryoverReadback(pending)
  const next = prepareCarryoverReview(scope, changed, 'continue_today', '孩子确认', 'fresh-turn', pending.confirmationId)
  expect(() => finishCarryoverReview(original, receipt(original))).toThrow()
  expect(readCarryoverReview(scope.sessionId, scope.childId)).toEqual(next)
})

it('all mutation helpers preserve a same-session record owned by a different child', () => {
  const original = prepare()
  const other = { ...original, childId: 'another-child' }
  localStorage.setItem(key, JSON.stringify(other))
  for (const mutate of [
    () => finishCarryoverReview(original, receipt(original)),
    () => invalidateCarryoverReview(original, details(original)),
    () => markCarryoverReadback(original),
    () => refreshCarryoverReadback({ ...original, confirmationId: 'obsolete' }, changed),
    () => prepare(),
  ]) expect(mutate).toThrow()
  expect(localStorage.getItem(key)).toBe(JSON.stringify(other))
  expect(carryoverConfirmationAtArrival(scope.sessionId, scope.childId)).toBeNull()
})

it.each(['null', '[]', '"text"', '{bad'])('malformed stored %s is retained and cannot select a new first task', raw => {
  localStorage.setItem(key, raw)
  expect(prepare).toThrow()
  expect(localStorage.getItem(key)).toBe(raw)
  expect(carryoverConfirmationAtArrival(scope.sessionId, scope.childId)).toBeNull()
})
