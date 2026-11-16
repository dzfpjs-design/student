import { describe, expect, it } from 'vitest'
import { hasPcmTailRendered } from './pcmTailTiming'

const sampleRates = [8_000, 16_000, 22_050, 24_000, 32_000, 44_100, 48_000]
// LCM of the sample rates and 50: samples and the 20 ms scheduling offset
// are exact integers on this independent reference timeline.
const ticksPerSecond = 14_112_000n
const headroomTicks = ticksPerSecond / 50n
const frameCounts = [1, 127, 239, 480, 997, 2047, 8000]
const seconds = (ticks: bigint): number => Number(ticks) / Number(ticksPerSecond)

function verifySampleTimeline(rates: number[], reanchor: boolean): number {
  let referenceStart = 0n
  let scheduledStart = 0
  let oldComparisonFalseRejections = 0
  for (let ordinal = 1; ordinal <= 600; ordinal++) {
    const rate = rates[(ordinal - 1) % rates.length]
    const frames = frameCounts[(ordinal - 1) % frameCounts.length]
    const durationTicks = BigInt(frames) * (ticksPerSecond / BigInt(rate))
    let clockTicks = referenceStart > 2n * headroomTicks ? referenceStart - 2n * headroomTicks : 0n
    if (reanchor && ordinal % 37 === 0) clockTicks = referenceStart + headroomTicks
    const earliestStart = clockTicks + headroomTicks
    if (referenceStart < earliestStart) referenceStart = earliestStart
    scheduledStart = Math.max(scheduledStart, seconds(clockTicks) + 0.02)

    const referenceEnd = referenceStart + durationTicks
    const nativeEndClock = seconds(referenceEnd)
    const scheduledEnd = scheduledStart + frames / rate
    const outputRate = Math.max(...rates)
    // Expected completion comes from exact sample counts, not a second copy
    // of the implementation's floating-point allowance formula.
    expect(hasPcmTailRendered(nativeEndClock, scheduledEnd, ordinal, outputRate)).toBe(true)
    expect(hasPcmTailRendered(nativeEndClock - 1 / outputRate, scheduledEnd, ordinal, outputRate)).toBe(false)
    expect(hasPcmTailRendered(nativeEndClock - 1 / (4 * outputRate), scheduledEnd, ordinal, outputRate)).toBe(false)
    if (nativeEndClock + Number.EPSILON * Math.max(1, scheduledEnd) < scheduledEnd) oldComparisonFalseRejections++

    referenceStart = referenceEnd
    scheduledStart += frames / rate
  }
  return oldComparisonFalseRejections
}

describe('PCM ended-clock numerical comparison', () => {
  it.each(sampleRates)('accepts 1..600 exact sample tails at %i Hz while rejecting genuine early ends', rate => {
    verifySampleTimeline([rate], false)
  })

  it('covers mixed source rates against one exact common tick timeline', () => {
    expect(verifySampleTimeline(sampleRates, false)).toBeGreaterThan(0)
  })

  it('covers repeated clock/headroom reanchoring without resetting source ordinals', () => {
    verifySampleTimeline(sampleRates, true)
  })

  it.each([2, 3])('accepts a recorded F51 deficit size of %i binary ulps at a synthetic 4-second clock', ulps => {
    const scheduledEnd = 4 + ulps * 2 ** -50
    expect(4 + Number.EPSILON * scheduledEnd < scheduledEnd).toBe(true)
    expect(hasPcmTailRendered(4, scheduledEnd, 35, 24_000)).toBe(true)
  })

  it('does not give even a 100 ns early callback a fixed time grace', () => {
    expect(hasPcmTailRendered(300 - 1e-7, 300, 600, 48_000)).toBe(false)
  })

  it('rejects an excessive uncertainty rather than clamping it into a sub-sample grace', () => {
    // This representable gap is smaller than an eighth of a 48 kHz sample,
    // but the enormous timeline makes its numerical uncertainty unacceptable.
    expect(hasPcmTailRendered(2 ** 30 - 2 ** -22, 2 ** 30, 600, 48_000)).toBe(false)
  })

  it('uses the stricter output sample rate when the error budget approaches the sample cap', () => {
    const end = 2 ** 24
    const before = end - 2 ** -28
    expect(hasPcmTailRendered(before, end, 600, 24_000)).toBe(true)
    expect(hasPcmTailRendered(before, end, 600, 48_000)).toBe(false)
  })

  it('rejects an ordinal whose accumulated-error estimate is outside the bounded comparison domain', () => {
    expect(hasPcmTailRendered(2 - 1e-10, 2, Number.MAX_SAFE_INTEGER, 24_000)).toBe(false)
  })

  it.each([0, -1, 0.5, Number.NaN, Number.POSITIVE_INFINITY, Number.MAX_SAFE_INTEGER + 1])(
    'rejects invalid source ordinal %s even at the exact tail', ordinal => {
      expect(hasPcmTailRendered(2, 2, ordinal, 24_000)).toBe(false)
    },
  )

  it.each([
    [Number.NaN, 2], [Number.POSITIVE_INFINITY, 2], [-0.001, 2],
    [2, Number.NaN], [2, Number.POSITIVE_INFINITY], [2, -0.001],
  ])('rejects invalid clock/tail values (%s, %s)', (clock, tail) => {
    expect(hasPcmTailRendered(clock, tail, 1, 24_000)).toBe(false)
  })

  it.each([0, -1, Number.NaN, Number.POSITIVE_INFINITY])('rejects invalid sample rate %s', rate => {
    expect(hasPcmTailRendered(2, 2, 1, rate)).toBe(false)
  })

  it('still accepts a finite native callback at or after its scheduled tail', () => {
    expect(hasPcmTailRendered(0, 0, 1, 48_000)).toBe(true)
    expect(hasPcmTailRendered(2, 2, 1, 48_000)).toBe(true)
    expect(hasPcmTailRendered(2.1, 2, 1, 48_000)).toBe(true)
  })
})
