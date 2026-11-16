import { describe, expect, it } from 'vitest'
import { effectiveActiveMilliseconds, effectiveActiveMinutes } from './studyTime'

describe('effective study time', () => {
  it('adds only the current active segment to persisted focused time', () => {
    const now = Date.parse('2026-08-26T12:10:00.000Z')
    const item = {
      status: 'active',
      active_seconds: 300,
      active_segment_started_at: '2026-08-26T12:08:30.000Z'
    }

    expect(effectiveActiveMilliseconds(item, now)).toBe(390_000)
    expect(effectiveActiveMinutes(item, now)).toBe(7)
  })

  it('does not count wall-clock time while a task is paused', () => {
    const item = {
      status: 'paused',
      active_seconds: 420,
      active_segment_started_at: '2026-08-26T10:00:00.000Z'
    }

    expect(effectiveActiveMinutes(item, Date.parse('2026-08-26T14:00:00.000Z'))).toBe(7)
  })
})
