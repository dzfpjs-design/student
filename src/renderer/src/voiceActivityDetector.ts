export type VoiceActivityObservation = { voiced: boolean; speechDetected: boolean; threshold: number; noiseFloor: number }
/** Learns the room floor and requires sustained energy before treating input as speech. */
export class AdaptiveVoiceActivityDetector {
  private noiseFloor = 0.0025
  private candidateRun = 0
  private candidateMs = 0
  private speechDetected = false

  observe(value: number, durationMs = 32): VoiceActivityObservation {
    const rms = Number.isFinite(value) ? Math.max(0, value) : 0
    const duration = Number.isFinite(durationMs) && durationMs > 0 ? durationMs : 0
    const threshold = Math.min(0.018, Math.max(0.0025, this.noiseFloor * 1.6))
    const voiced = duration > 0 && rms >= threshold && rms >= this.noiseFloor + 0.0006
    this.candidateRun = voiced ? this.candidateRun + 1 : 0
    this.candidateMs = voiced ? this.candidateMs + duration : 0
    if (this.candidateRun >= 3 && this.candidateMs >= 96) this.speechDetected = true
    if (!voiced && !this.speechDetected) {
      this.noiseFloor = this.noiseFloor * 0.92 + Math.min(rms, threshold) * 0.08
    }
    return { voiced, speechDetected: this.speechDetected, threshold, noiseFloor: this.noiseFloor }
  }

  reset(): void {
    this.candidateRun = 0
    this.candidateMs = 0
    this.speechDetected = false
  }
}

