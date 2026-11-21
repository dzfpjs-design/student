import type { DocumentFrameFeatures } from './documentFrameMonitor'

/** A page boundary isolates candidates; it never certifies that two papers are identical. */
export class DocumentPreviewPageEpoch {
  private source?: string
  private frameToken?: number
  private pageToken?: string
  private previous?: DocumentFrameFeatures
  private missingSince?: number
  private missingBoundary = false

  constructor(private readonly newToken: () => string = () => crypto.randomUUID()) {}

  reset(): void {
    this.source = undefined
    this.frameToken = undefined
    this.pageToken = undefined
    this.previous = undefined
    this.missingSince = undefined
    this.missingBoundary = false
  }

  observe(source: string, frameToken: number, at: number, frame: DocumentFrameFeatures, pageChanged = false): string {
    const newSource = source !== this.source || this.frameToken !== undefined && frameToken < this.frameToken
    if (newSource) this.reset()
    if (!this.pageToken) this.pageToken = this.newToken()
    if (this.source === source && this.frameToken === frameToken) return this.pageToken
    const missing = frame.paperRatio < .1 || frame.brightness < 28
    if (missing) this.missingSince ??= at
    else { this.missingSince = undefined; this.missingBoundary = false }
    const missingBoundary = this.missingSince !== undefined && at - this.missingSince >= 1_000 && !this.missingBoundary
    // Observe broad changes even before a formal page has ever been captured.
    // Local writing/partial hand occlusion need not start a different page.
    const broadChange = !newSource && this.previous && frame.skinRatio < .025 && this.previous.skinRatio < .025
      && frame.paperRatio >= .2 && this.previous.paperRatio >= .2
      && distance(frame.fingerprint, this.previous.fingerprint) >= 18
    if (!newSource && (pageChanged || missingBoundary || broadChange)) this.pageToken = this.newToken()
    if (missingBoundary) this.missingBoundary = true
    this.source = source
    this.frameToken = frameToken
    this.previous = { ...frame, fingerprint: frame.fingerprint.slice() }
    return this.pageToken
  }
}

function distance(a: Uint8Array, b: Uint8Array): number {
  if (a.length !== b.length || !a.length) return Infinity
  return a.reduce((sum, value, index) => sum + Math.abs(value - b[index]), 0) / a.length
}
