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
    // Spatial changes can isolate a page before any formal capture. Warm paper
    // and incomplete framing cannot establish or rule out a page boundary.
    const broadChange = !newSource && this.previous && hasBroadSpatialChange(frame.fingerprint, this.previous.fingerprint)
    if (!newSource && (pageChanged || missingBoundary || broadChange)) this.pageToken = this.newToken()
    if (missingBoundary) this.missingBoundary = true
    this.source = source
    this.frameToken = frameToken
    this.previous = { ...frame, fingerprint: frame.fingerprint.slice() }
    return this.pageToken
  }
}

function hasBroadSpatialChange(a: Uint8Array, b: Uint8Array): boolean {
  const rows = 6, columns = 8, cells = rows * columns
  if (a.length !== cells || b.length !== cells) return false
  const changes = Array.from(a, (value, index) => value - b[index])
  // Remove the camera's global exposure offset. An observed middle value,
  // rather than the mean of two values, avoids inventing residual changes in
  // both halves when exactly half the frame is covered by a stationary hand.
  const exposureShift = [...changes].sort((left, right) => left - right)[Math.floor(cells / 2)]
  let strongCells = 0, totalResidual = 0
  const quadrants = new Set<number>()
  for (let index = 0; index < cells; index += 1) {
    const residual = Math.abs(changes[index] - exposureShift)
    totalResidual += residual
    if (residual < 25) continue
    strongCells += 1
    const row = Math.floor(index / columns), column = index % columns
    quadrants.add((row >= rows / 2 ? 2 : 0) + (column >= columns / 2 ? 1 : 0))
  }
  // This is a conservative isolation boundary, never proof of physical page
  // identity. A local mark/hand and an exposure-only shift stay in one group.
  return strongCells >= Math.ceil(cells * .35) && quadrants.size >= 3 && totalResidual / cells >= 18
}
