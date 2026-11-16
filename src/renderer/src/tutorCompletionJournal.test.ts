import { afterEach, expect, it, vi } from 'vitest'
import { clearTutorCompletion, readTutorCompletions, saveTutorCompletion, waitForTutorReceipt, type TutorCompletion } from './tutorCompletionJournal'
import type { TutorTurn } from './types'

const result = (): TutorCompletion => ({ version: 1, sessionId: 'session-a', childId: 'child-a', turnId: 'agent-turn-a', actionId: 'hint-action-a',
  response: '仅在本机解释的课程内容。', draftText: '原问题', localOnly: true, acknowledged: false, presentationStarted: false,
  tutor: { turn_id: 'tutor-a', session_id: 'session-a', child_id: 'child-a', answer: '仅在本机解释的课程内容。' } as TutorTurn,
  receipt: { capability: 'tutor.more_hint', execution_location: 'client_device_bridge', action_type: 'more_tutor_hint', status: 'completed',
    completed_at: '2026-09-07T10:00:00Z', output: { tutor_turn_id: 'tutor-a', blackboard_present: false } } })
afterEach(() => { localStorage.clear(); vi.useRealTimers(); vi.restoreAllMocks() })

it('keeps the actual tutor result, fixed receipt and local-only policy through serialization', () => {
  const item = result()
  saveTutorCompletion(item)
  const restored = readTutorCompletions('session-a', 'child-a')[0]
  expect(restored).toEqual(item)
  saveTutorCompletion({ ...restored, acknowledged: true, presentationStarted: true })
  expect(readTutorCompletions('session-a', 'child-a')[0].receipt).toEqual(item.receipt)
})

it('isolates sessions and rejects cross-child result evidence without deleting it', () => {
  saveTutorCompletion(result())
  expect(readTutorCompletions('session-b', 'child-b')).toEqual([])
  expect(() => readTutorCompletions('session-a', 'child-b')).toThrow('TUTOR_COMPLETION_INVALID')
  expect(readTutorCompletions('session-a', 'child-a')).toHaveLength(1)
})

it('does not accept a changed capability as an acknowledgement-only recovery', () => {
  const item = result()
  item.receipt.capability = 'profile.switch'
  saveTutorCompletion(item)
  expect(() => readTutorCompletions('session-a', 'child-a')).toThrow('TUTOR_COMPLETION_INVALID')
})

it('keeps result evidence when local deletion fails', () => {
  const item = result()
  saveTutorCompletion(item)
  vi.spyOn(Storage.prototype, 'removeItem').mockImplementationOnce(() => { throw new Error('disk busy') })
  expect(() => clearTutorCompletion(item)).toThrow('disk busy')
  expect(readTutorCompletions('session-a', 'child-a')[0]).toEqual(item)
})

it('bounds acknowledgement waiting without treating a late network response as failure evidence', async () => {
  vi.useFakeTimers()
  let resolve!: (value: string) => void
  const request = new Promise<string>(done => { resolve = done })
  const waiting = waitForTutorReceipt(request)
  const rejected = expect(waiting).rejects.toThrow('TUTOR_RECEIPT_UNCONFIRMED')
  await vi.advanceTimersByTimeAsync(10_000)
  await rejected
  resolve('completed')
  expect(await request).toBe('completed')
})
