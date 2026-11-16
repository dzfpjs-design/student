export type FrameHealthStatus = 'healthy' | 'too_dark' | 'too_bright' | 'low_contrast'

export type FrameHealthResult = {
  status: FrameHealthStatus
  message: string
}

// Local observation samples a frame every two seconds. Pixel similarity is
// never stream-health evidence: a child can read or write in a fixed pose for
// minutes. Playback progress and track state are checked separately by
// CameraFrameClock.
export const FRAME_HEALTH_SAMPLE_INTERVAL_MS = 2_000
/**
 * Keeps only a tiny in-memory luminance fingerprint for stream health.  It is
 * never persisted, rendered, uploaded, or used to identify a person.
 */
export class FrameHealthMonitor {
  reset(): void {}

  inspect(frame: Pick<ImageData, 'data' | 'width' | 'height'>): FrameHealthResult {
    const sample = luminanceFingerprint(frame)
    const average = sample.reduce((total, value) => total + value, 0) / Math.max(sample.length, 1)
    const variance = sample.reduce((total, value) => total + (value - average) ** 2, 0) / Math.max(sample.length, 1)
    const buckets = new Map<number, number>()
    sample.forEach((value) => {
      const bucket = Math.floor(value / 8)
      buckets.set(bucket, (buckets.get(bucket) ?? 0) + 1)
    })
    const dominantRatio = Math.max(0, ...buckets.values()) / Math.max(sample.length, 1)
    if (average <= 12) return { status: 'too_dark', message: '画面接近全黑，当前取景不可用于观察。' }
    if (average >= 243) return { status: 'too_bright', message: '画面接近全白或过曝，当前取景不可用于观察。' }
    if (variance < 18 || dominantRatio >= 0.92) return {
      status: 'low_contrast',
      message: '画面接近纯色，可能是摄像头隐私挡板关闭、镜头被遮住或驱动只返回占位画面，当前不能用于观察。'
    }

    return { status: 'healthy', message: '画面健康。' }
  }
}

function luminanceFingerprint(frame: Pick<ImageData, 'data' | 'width' | 'height'>): Uint8Array {
  const columns = 12
  const rows = 8
  const output = new Uint8Array(columns * rows)
  for (let row = 0; row < rows; row += 1) {
    for (let column = 0; column < columns; column += 1) {
      const x = Math.min(frame.width - 1, Math.floor((column + 0.5) * frame.width / columns))
      const y = Math.min(frame.height - 1, Math.floor((row + 0.5) * frame.height / rows))
      const offset = (y * frame.width + x) * 4
      output[row * columns + column] = Math.round(
        frame.data[offset] * 0.2126 + frame.data[offset + 1] * 0.7152 + frame.data[offset + 2] * 0.0722
      )
    }
  }
  return output
}


// L523 can repeat a detailed image while its transport counters keep advancing.
// Exact repeated pixels are uncertainty, not proof of a frozen device or person.
// Content changes restore analysis but never refill the authorization budget.
// Fifteen consecutive two-second intervals permit one bounded source recheck.
export const L523_CONTENT_RECHECK_AFTER_MS = 30_000
export class L523ContentFreshnessMonitor {
  private pixels: Uint8ClampedArray | null = null
  private width = 0
  private height = 0
  private source: object | null = null
  private unchangedSince = 0
  private sampledAt = 0
  private blocked = false
  private sourceCheckSpent = false
  get unconfirmed(): boolean { return this.blocked }
  reset(): void { this.clearContent(); this.sourceCheckSpent = false }
  clearContent(): void {
    this.pixels = null; this.width = 0; this.height = 0; this.source = null
    this.unchangedSince = 0; this.sampledAt = 0; this.blocked = false
  }
  inspect(frame: Pick<ImageData, 'data' | 'width' | 'height'>, source: object, now = performance.now()): { unconfirmed: boolean; recheckSource: boolean } {
    const same = this.pixels !== null && this.width === frame.width && this.height === frame.height
      && this.pixels.length === frame.data.length && this.pixels.every((value, index) => value === frame.data[index])
    const sourceChanged = this.source !== source
    if (this.blocked && sourceChanged) {
      // A replacement may begin with another cached/exposure-adjusted image.
      // It must change again on that same source before content is eligible.
      this.pixels = new Uint8ClampedArray(frame.data); this.width = frame.width; this.height = frame.height
      this.source = source; this.sampledAt = now
      return { unconfirmed: true, recheckSource: false }
    }
    if (!same) {
      this.pixels = new Uint8ClampedArray(frame.data); this.width = frame.width; this.height = frame.height
      this.unchangedSince = now; this.blocked = false
    } else if (!this.blocked && (sourceChanged || now < this.sampledAt || now - this.sampledAt > 5_000)) {
      // Unobserved time and a different source cannot earn a repeat interval.
      this.unchangedSince = now
    }
    this.source = source; this.sampledAt = now
    if (!this.blocked && now - this.unchangedSince >= L523_CONTENT_RECHECK_AFTER_MS) {
      this.blocked = true
      const recheckSource = !this.sourceCheckSpent
      this.sourceCheckSpent = true
      return { unconfirmed: true, recheckSource }
    }
    // Keep the same in-memory pixels through the one replacement source. An
    // unchanged static scene waits for changed content or normal explicit retry;
    // it cannot cause an endless reconnect cycle or a behavior conclusion.
    return { unconfirmed: this.blocked, recheckSource: false }
  }
}
export function isL523ObservationSource(label: string): boolean {
  return /^L523(?:\s|\(|$)/i.test(label.trim())
}
