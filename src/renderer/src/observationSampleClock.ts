export type ObservationCaptureSample = {
  stream_epoch: string
  sequence: number
  captured_at: string
  capture_monotonic_ms: number
}

export class ObservationSampleClock {
  readonly epoch = crypto.randomUUID()
  sequence = 0
  private lastMediaTime = -1

  sample(mediaTime: number, monotonicMs = performance.now(), at = new Date()): ObservationCaptureSample | undefined {
    if (!Number.isFinite(mediaTime) || mediaTime <= this.lastMediaTime || !Number.isFinite(monotonicMs) || monotonicMs < 0) return
    this.lastMediaTime = mediaTime
    return { stream_epoch: this.epoch, sequence: ++this.sequence,
      captured_at: at.toISOString(), capture_monotonic_ms: Math.floor(monotonicMs) }
  }
}
