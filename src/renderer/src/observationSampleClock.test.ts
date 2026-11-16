import { describe, expect, it } from 'vitest'
import { ObservationSampleClock } from './observationSampleClock'

describe('fresh camera sample identity', () => {
  it('never counts the same video frame or backward clock twice', () => {
    const clock = new ObservationSampleClock()
    expect(clock.sample(0, 100)?.sequence).toBe(1)
    expect(clock.sample(0, 200)).toBeUndefined()
    expect(clock.sample(-1, 300)).toBeUndefined()
    expect(clock.sample(.033, 400)?.sequence).toBe(2)
    expect(clock.sample(NaN)).toBeUndefined()
  })
  it('creates a separate epoch after each stream reopen', () => {
    const a = new ObservationSampleClock(), b = new ObservationSampleClock()
    expect(a.epoch).not.toBe(b.epoch)
    expect(a.sample(12)?.sequence).toBe(1)
    expect(b.sample(0)?.sequence).toBe(1)
  })
})
