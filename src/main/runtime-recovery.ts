export const RENDERER_RECOVERY_BASE_DELAY_MS = 500
export const RENDERER_RECOVERY_MAX_DELAY_MS = 8_000
export const RENDERER_RECOVERY_WINDOW_MS = 2 * 60_000
export const RENDERER_RECOVERY_MAX_ATTEMPTS = 5

export class BoundedRendererRecovery {
  private failures: number[] = []

  nextDelay(now = Date.now()): number | null {
    this.failures = this.failures.filter((at) => now - at <= RENDERER_RECOVERY_WINDOW_MS)
    if (this.failures.length >= RENDERER_RECOVERY_MAX_ATTEMPTS) return null
    this.failures.push(now)
    return Math.min(
      RENDERER_RECOVERY_BASE_DELAY_MS * (2 ** (this.failures.length - 1)),
      RENDERER_RECOVERY_MAX_DELAY_MS
    )
  }

  markStable(): void {
    this.failures = []
  }
}
