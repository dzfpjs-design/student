export const REALTIME_AUDIO_ACTIVITY_TIMEOUT_MS = 30_000

export type AudioActivitySample = {
  sourceId: string
  energy: number
  duration: number
}

/** Cumulative audio statistics are a transport cue, never speaker/word evidence.
 * A timeout may also mean deliberately ignored noise. It only retires the
 * uncertain connection; it cannot admit input, replay audio or execute work. */
export class RealtimeAudioActivityHealth {
  private previous: (AudioActivitySample & { at: number }) | null = null
  private lastActivityAt: number | null = null
  private knownInterval = false

  reset(): void { this.previous = null; this.lastActivityAt = null; this.knownInterval = false }

  /** Anonymous transport state for postponing maintenance, never input admission. */
  snapshot(at: number): { known: boolean; fresh: boolean; pending: boolean } {
    const fresh = this.previous !== null && Number.isFinite(at) && at >= this.previous.at
      && at - this.previous.at <= 2_000
    return { known: this.knownInterval, fresh, pending: this.lastActivityAt !== null }
  }

  observe(sample: AudioActivitySample | null, at: number): boolean {
    if (!sample || !sample.sourceId || !Number.isFinite(at)
      || !Number.isFinite(sample.energy) || !Number.isFinite(sample.duration)
      || sample.energy < 0 || sample.duration < 0) { this.reset(); return false }
    const previous = this.previous
    this.previous = { ...sample, at }
    this.knownInterval = false
    if (!previous || sample.sourceId !== previous.sourceId || at < previous.at
      || at - previous.at > 5_000 || sample.energy < previous.energy || sample.duration < previous.duration) {
      this.lastActivityAt = null
      return false
    }
    const duration = sample.duration - previous.duration
    const energy = sample.energy - previous.energy
    if (duration > 5) { this.lastActivityAt = null; return false }
    this.knownInterval = duration >= 0.1
    if (duration >= 0.1 && Math.sqrt(energy / duration) >= 0.03) this.lastActivityAt = at
    return this.lastActivityAt !== null && at - this.lastActivityAt >= REALTIME_AUDIO_ACTIVITY_TIMEOUT_MS
  }
}
