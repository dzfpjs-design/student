/** Sample-bracket estimates only: never proof of attention, work or continuous observation. */
export type DocumentVisibilitySample = {
  /** Read from getVideoPlaybackQuality().totalVideoFrames; no currentTime fallback. */
  frameCount: number
  /** performance.now(), from the same renderer lifetime. */
  tickMs: number
  generation: number
  /** Persisted revision ID for a locally matched page, never image pixels/fingerprints. */
  pageRevisionId: string | null
  /** True only for a readable, unoccluded frame and a live, authorised monitor. */
  qualityOk: boolean
  /** True only if this frame matches that revision using the existing in-memory matcher. */
  pageMatches: boolean
}

export type DocumentVisibilitySegment = {
  generation: number
  pageRevisionId: string
  startTickMs: number
  endTickMs: number
  estimatedMs: number
  intervalCount: number
  interpretation: 'sample_bracket_estimate_not_continuous_proof'
}

/** One monotonic clock/lifetime. A caller must flush at pause, stop or unmount. */
export class DocumentVisibilityEstimator {
  private previous: DocumentVisibilitySample | null = null
  private segment: DocumentVisibilitySegment | null = null

  constructor(private readonly maxGapMs = 1500, private readonly maxSegmentMs = 30_000) {
    if (!Number.isFinite(maxGapMs) || maxGapMs <= 0 || maxGapMs > 5000 ||
        !Number.isFinite(maxSegmentMs) || maxSegmentMs < maxGapMs || maxSegmentMs > 60_000) {
      throw new Error('INVALID_VISIBILITY_LIMITS')
    }
  }

  observe(sample: DocumentVisibilitySample): DocumentVisibilitySegment | null {
    const valid = Number.isSafeInteger(sample.frameCount) && sample.frameCount >= 0 &&
      Number.isSafeInteger(sample.generation) && sample.generation >= 0 &&
      Number.isFinite(sample.tickMs) && sample.tickMs >= 0 && sample.qualityOk === true &&
      sample.pageMatches === true && typeof sample.pageRevisionId === 'string' &&
      sample.pageRevisionId.trim().length > 0 && sample.pageRevisionId.length <= 128
    if (!valid) return this.stop()
    const prior = this.previous
    if (!prior) { this.previous = { ...sample }; return null }
    const delta = sample.tickMs - prior.tickMs
    const adjacent = prior.generation === sample.generation && prior.pageRevisionId === sample.pageRevisionId &&
      sample.frameCount > prior.frameCount && delta > 0 && delta <= this.maxGapMs
    if (!adjacent || (this.segment && this.segment.estimatedMs + delta > this.maxSegmentMs)) {
      const result = this.stop()
      this.previous = { ...sample }
      return result
    }
    if (!this.segment) {
      this.segment = { generation: sample.generation, pageRevisionId: sample.pageRevisionId!,
        startTickMs: prior.tickMs, endTickMs: sample.tickMs, estimatedMs: delta,
        intervalCount: 1, interpretation: 'sample_bracket_estimate_not_continuous_proof' }
    } else {
      this.segment.endTickMs = sample.tickMs
      this.segment.estimatedMs += delta
      this.segment.intervalCount += 1
    }
    this.previous = { ...sample }
    return null
  }

  stop(): DocumentVisibilitySegment | null {
    const result = this.segment ? { ...this.segment } : null
    this.segment = null
    this.previous = null
    return result
  }
}
