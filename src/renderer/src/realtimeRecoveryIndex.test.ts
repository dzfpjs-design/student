import { afterEach, describe, expect, it } from 'vitest'
import { pendingRealtimeAdmissions, prepareRealtimeAdmission, restoreRealtimeAdmissionIdentities, updateRealtimeAdmission, type RealtimeRecoveryIndex } from './realtimeAdmissionJournal'

afterEach(() => localStorage.clear())
const index = (): RealtimeRecoveryIndex => ({ session_id: 'session', child_id: 'child', entries: [
  { client_action_id: 'original-first', voice_input_id: 'voice-first', status: 'completed', created_at: '2026-10-06T00:00:00.001Z' },
  { client_action_id: 'original-second', voice_input_id: null, status: 'prepared', created_at: '2026-10-06T00:00:00.002Z' },
], has_more: false, local_only: true, cloud_transmission_allowed: false, replay_allowed: false, mastery_update_allowed: false })
describe('canonical recovery identity projection', () => {
  it('recreates only missing metadata in canonical order without speech or execution', () => {
    expect(restoreRealtimeAdmissionIdentities('session', 'child', index())).toBe(2)
    expect(pendingRealtimeAdmissions('session', 'child').map(row => [row.inputId, row.phase, row.voiceInputId]))
      .toEqual([['original-first', 'stored', 'voice-first'], ['original-second', 'admitting', null]])
    expect(restoreRealtimeAdmissionIdentities('session', 'child', index())).toBe(0)
    expect(pendingRealtimeAdmissions('session', 'other-child')).toEqual([])
  })
  it('does not overwrite a current cancellation or dispatching owner', () => {
    const original = updateRealtimeAdmission(prepareRealtimeAdmission('session', 'child', 'original-first'), 'cancel_requested')
    restoreRealtimeAdmissionIdentities('session', 'child', index())
    expect(pendingRealtimeAdmissions('session', 'child').find(row => row.inputId === original.inputId)).toEqual(original)
  })
  it.each(['child', 'session', 'permission', 'duplicate', 'malformed', 'extra-text', 'bad-time'])('rejects %s before writing any new identity', fault => {
    const value = index()
    if (fault === 'child') value.child_id = 'other'
    if (fault === 'session') value.session_id = 'other'
    if (fault === 'permission') Object.assign(value, { replay_allowed: true })
    if (fault === 'duplicate') value.entries.push(value.entries[0])
    if (fault === 'malformed') value.entries[1].voice_input_id = 'not-null'
    if (fault === 'extra-text') Object.assign(value.entries[1], { transcript: 'private detail' })
    if (fault === 'bad-time') value.entries[1].created_at = 'invalid'
    expect(() => restoreRealtimeAdmissionIdentities('session', 'child', value)).toThrow()
    expect(pendingRealtimeAdmissions('session', 'child')).toEqual([])
  })
  it('rejects a conflicting voice identity without changing the original handoff', () => {
    const old = updateRealtimeAdmission(prepareRealtimeAdmission('session', 'child', 'original-first'), 'stored', 'different-voice')
    expect(() => restoreRealtimeAdmissionIdentities('session', 'child', index())).toThrow()
    expect(pendingRealtimeAdmissions('session', 'child')).toEqual([old])
  })
})
