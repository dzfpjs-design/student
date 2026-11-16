import { describe, expect, it } from 'vitest'
import { DocumentVisibilityEstimator, type DocumentVisibilitySample } from './documentVisibilityEstimator'

const sample = (tickMs: number, frameCount: number, overrides: Partial<DocumentVisibilitySample> = {}): DocumentVisibilitySample => ({
  tickMs, frameCount, generation: 1, pageRevisionId: 'revision-1', qualityOk: true, pageMatches: true, ...overrides
})

describe('sample-bracket document visibility estimates', () => {
  it('counts only bounded adjacent advances and marks estimates accurately', () => {
    const v = new DocumentVisibilityEstimator()
    expect(v.observe(sample(0, 5))).toBeNull()
    expect(v.observe(sample(500, 10))).toBeNull()
    expect(v.observe(sample(1000, 20))).toBeNull()
    expect(v.stop()).toEqual({ generation: 1, pageRevisionId: 'revision-1', startTickMs: 0, endTickMs: 1000,
      estimatedMs: 1000, intervalCount: 2, interpretation: 'sample_bracket_estimate_not_continuous_proof' })
    expect(v.stop()).toBeNull()
  })
  it.each([
    ['frozen frame', sample(1000, 10)],
    ['reversed frame counter', sample(1000, 1)],
    ['occlusion', sample(1000, 20, { qualityOk: false })],
    ['unmatched page', sample(1000, 20, { pageMatches: false })],
    ['no page identity', sample(1000, 20, { pageRevisionId: null })],
    ['sampling gap', sample(2501, 20)],
    ['new page', sample(1000, 20, { pageRevisionId: 'revision-2' })],
    ['new generation', sample(1000, 20, { generation: 2 })],
    ['clock rollback', sample(10, 20)],
    ['invalid clock', sample(NaN, 20)],
  ])('ends the old segment on %s without charging the gap', (_label, next) => {
    const v = new DocumentVisibilityEstimator()
    v.observe(sample(0, 5)); v.observe(sample(500, 10))
    expect(v.observe(next)?.estimatedMs).toBe(500)
    expect(v.stop()).toBeNull()
  })
  it('caps segments and never returns a one-frame or pause-to-resume duration', () => {
    const v = new DocumentVisibilityEstimator(1000, 2000)
    v.observe(sample(0, 1)); v.observe(sample(1000, 2)); v.observe(sample(2000, 3))
    expect(v.observe(sample(3000, 4))?.estimatedMs).toBe(2000)
    expect(v.stop()).toBeNull()
    v.observe(sample(9999, 5)); expect(v.stop()).toBeNull()
  })
})
