import { beforeEach, expect, it, vi } from 'vitest'
import { finishCarryoverReview, prepareCarryoverReview, readCarryoverReview, invalidateCarryoverReview,
  markCarryoverReadback, carryoverConfirmationAtArrival, refreshCarryoverReadback } from './pendingCarryoverReview'
import type { DeferredTaskCandidate } from './types'

const scope = {childId: 'child-a', sessionId: 'session-a'}
const candidate = {source_plan_item_id: 'task-a', source_version: 'a'.repeat(64), title:'page A', subject:'数学'} as DeferredTaskCandidate
beforeEach(() => { localStorage.clear(); vi.restoreAllMocks() })
const prepare = () => prepareCarryoverReview(scope, candidate, 'continue_today', 'continue original', 'turn-a')

it('reopens the original persisted request rather than selecting the new first task', () => {
  const first = prepare()
  expect(readCarryoverReview(scope.sessionId, scope.childId)).toEqual(first)
  expect(prepareCarryoverReview(scope, {...candidate, source_plan_item_id:'task-b'},
    'continue_today', 'continue original', 'turn-b')).toEqual(first)
  expect(prepareCarryoverReview(scope, undefined, 'continue_today', 'continue original', 'turn-c')).toEqual(first)
})

it('cannot change a pending decision or read it as another child', () => {
  prepare()
  expect(() => prepareCarryoverReview(scope, candidate, 'cancelled', 'cancel', 'turn-b')).toThrow()
  expect(() => readCarryoverReview(scope.sessionId, 'child-b')).toThrow()
})

it('keeps original identity on a mismatched receipt and clears only the exact saved receipt', () => {
  const p = prepare()
  const receipt = {review_id:'review-a', source_plan_item_id:p.sourcePlanItemId, source_session_id:'old-session',
    child_id:p.childId, target_session_id:p.sessionId, decision:p.decision, reason:p.reason,
    target_homework_id:'new-homework', reviewed_at:'2026-09-09T00:00:00Z'}
  expect(() => finishCarryoverReview(p, {...receipt, source_plan_item_id:'task-b'})).toThrow()
  expect(readCarryoverReview(p.sessionId, p.childId)).toEqual(p)
  finishCarryoverReview(p, receipt)
  expect(readCarryoverReview(p.sessionId, p.childId)).toBeNull()
})

it('does not create a request when persistence or version proof is unavailable', () => {
  expect(() => prepareCarryoverReview(scope, {...candidate, source_version:undefined}, 'cancelled', 'cancel', 'turn')).toThrow()
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('disk full') })
  expect(prepare).toThrow('disk full')
})

it('fails closed for corrupt recovery data', () => {
  localStorage.setItem('hsc-pending-carryover-v1:session-a', 'null')
  expect(() => readCarryoverReview(scope.sessionId, scope.childId)).toThrow()
})

it('requires a new arrival after changed-source readback and preserves the rejected request identity', () => {
  const original = prepare()
  const current = {...candidate, source_version:'b'.repeat(64), title:'revised page A'}
  invalidateCarryoverReview(original, {not_applied:true, session_id:scope.sessionId, child_id:scope.childId,
    client_action_id:original.requestId, candidate:current})
  const pending = readCarryoverReview(scope.sessionId, scope.childId)!
  expect(() => finishCarryoverReview(original, {review_id:'late-old', source_plan_item_id:original.sourcePlanItemId,
    source_session_id:'old-session', child_id:original.childId, target_session_id:original.sessionId,
    decision:original.decision, reason:original.reason, target_homework_id:'old-homework', reviewed_at:'2026-09-09'})).toThrow()
  expect(readCarryoverReview(scope.sessionId, scope.childId)).toEqual(pending)
  expect(carryoverConfirmationAtArrival(scope.sessionId, scope.childId)).toBeNull()
  expect(() => prepareCarryoverReview(scope, current, 'continue_today', 'continue original', 'turn-b')).toThrow()
  markCarryoverReadback(pending)
  expect(() => prepareCarryoverReview(scope, current, 'continue_today', 'continue original', 'queued-before-readback', null)).toThrow()
  const arrival = carryoverConfirmationAtArrival(scope.sessionId, scope.childId)
  const next = prepareCarryoverReview(scope, current, 'continue_today', 'continue original', 'fresh-confirmation', arrival)
  expect(next.sourcePlanItemId).toBe(original.sourcePlanItemId)
  expect(next.sourceVersion).toBe(current.source_version)
  expect(next.requestId).not.toBe(original.requestId)
  expect(localStorage.getItem(`hsc-pending-carryover-v1:${scope.sessionId}:invalidated:${original.requestId}`)).toBe(JSON.stringify(original))
  expect(() => invalidateCarryoverReview(original, {not_applied:true, session_id:scope.sessionId, child_id:scope.childId,
    client_action_id:original.requestId, candidate:current})).toThrow()
})

it('a second source change invalidates the readback without redirecting to another task', () => {
  const original = prepare()
  const current = {...candidate, source_version:'b'.repeat(64)}
  invalidateCarryoverReview(original, {not_applied:true, session_id:scope.sessionId, child_id:scope.childId,
    client_action_id:original.requestId, candidate:current})
  const pending = readCarryoverReview(scope.sessionId, scope.childId)!
  markCarryoverReadback(pending)
  const oldArrival = carryoverConfirmationAtArrival(scope.sessionId, scope.childId)
  expect(() => refreshCarryoverReadback(pending, {...current, source_plan_item_id:'task-b'})).toThrow()
  const latest = {...candidate, source_version:'c'.repeat(64)}
  const refreshed = refreshCarryoverReadback(pending, latest)
  markCarryoverReadback(pending)
  expect(carryoverConfirmationAtArrival(scope.sessionId, scope.childId)).toBeNull()
  expect(() => prepareCarryoverReview(scope, latest, 'cancelled', 'cancel original', 'turn-b', oldArrival)).toThrow()
  markCarryoverReadback(refreshed)
  expect(prepareCarryoverReview(scope, latest, 'cancelled', 'cancel original', 'turn-c',
    carryoverConfirmationAtArrival(scope.sessionId, scope.childId)).sourceVersion).toBe(latest.source_version)
})
