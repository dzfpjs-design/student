/** One finite ledger owned by the current playback, never a PCM or ASR result store. */
export const MAX_LOCAL_CONTROL_SEGMENTS = 12
const NEW_SEGMENT_QUIET_MS = 320
const RETRY_INTERVAL_MS = 750
export class PlaybackControlAdmission {
  private active = true
  private childId: string | null = null
  private segments = 0
  private retryAfter = 0
  private needsNewSegment = false
  private quietMs = 0
  private captureEpoch: symbol | null = null
  constructor(readonly id: string, readonly generation: number, readonly sessionId?: string) {}
  bind(id: string, generation: number, sessionId: string, childId: string): boolean {
    if (!this.active || id !== this.id || generation !== this.generation || sessionId !== this.sessionId || !childId) return false
    if (this.childId === null) this.childId = childId
    return this.childId === childId
  }
  admitFrame(epoch: symbol, voiced: boolean, durationMs: number, now: number): boolean {
    if (!this.active || this.childId === null || this.segments >= MAX_LOCAL_CONTROL_SEGMENTS) return false
    if (this.captureEpoch !== epoch) {
      // A new source cannot borrow prior quiet or restart midway through sound.
      if (this.captureEpoch !== null) this.needsNewSegment = true
      this.captureEpoch = epoch
      this.quietMs = 0
    }
    if (!this.needsNewSegment) return true
    this.quietMs = voiced ? 0 : this.quietMs + durationMs
    if (this.quietMs < NEW_SEGMENT_QUIET_MS || now < this.retryAfter) return false
    this.needsNewSegment = false
    return false
  }
  consume(epoch: symbol, now: number): boolean {
    if (!this.active || this.captureEpoch !== epoch || this.childId === null || this.needsNewSegment || this.segments >= MAX_LOCAL_CONTROL_SEGMENTS || now < this.retryAfter) return false
    this.segments += 1
    this.retryAfter = now + RETRY_INTERVAL_MS
    this.needsNewSegment = true
    this.quietMs = 0
    return true
  }
  dispose(): void { this.active = false; this.childId = null; this.captureEpoch = null; this.quietMs = 0 }
}
