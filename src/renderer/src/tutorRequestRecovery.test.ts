import { describe, expect, it, vi } from 'vitest'
import { parseTutorRequestRecovery, TUTOR_REQUEST_RECOVERY_MAX_AGE_MS } from './tutorRequestRecovery'

describe('durable tutor request recovery', () => {
  it('accepts only a recent request bound to this child and session', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-28T12:00:00Z'))
    const value = {
      version: 1, sessionId: 'session-1', childId: 'child-1', actionId: 'tutor-action-1',
      subject: '数学', question: '12加8等于多少？', hintLevel: 1,
      ocrConfirmationId: 'confirmation-1', createdAt: '2026-08-28T11:00:00Z'
    }
    expect(parseTutorRequestRecovery(value, 'session-1', 'child-1')).toEqual(value)
    expect(parseTutorRequestRecovery(value, 'session-other', 'child-1')).toBeNull()
    expect(parseTutorRequestRecovery({ ...value, childId: 'child-other' }, 'session-1', 'child-1')).toBeNull()
    expect(parseTutorRequestRecovery({ ...value, createdAt: new Date(Date.now() - TUTOR_REQUEST_RECOVERY_MAX_AGE_MS - 1).toISOString() }, 'session-1', 'child-1')).toBeNull()
    vi.useRealTimers()
  })
})
