import type { DocumentFrameFeatures } from './documentFrameMonitor'

export type DocumentPreviewPageBoundary = Readonly<{
  preview_boundary_reason: 'initial' | 'source_changed' | 'counter_reset' | 'blackout_or_page_missing' | 'page_changed_signal' | 'broad_spatial_change' | 'explicit_invalidation'
  strong_cells?: number
  quadrants?: number
  mean_residual?: number
  exposure_offset_abs?: number
  missing_elapsed_ms: number
}>
type SpatialChange = { changed: boolean; strong_cells: number; quadrants: number; mean_residual: number; exposure_offset_abs: number }

/** A page boundary isolates candidates; it never certifies that two papers are identical. */
export class DocumentPreviewPageEpoch {
  private source?: string
  private frameToken?: number
  private pageToken?: string
  private previous?: DocumentFrameFeatures
  private missingSince?: number
  private missingBoundary = false
  private nextCreationReason?: DocumentPreviewPageBoundary['preview_boundary_reason']

  constructor(private readonly newToken: () => string = () => crypto.randomUUID(),
    private readonly onBoundary?: (decision: DocumentPreviewPageBoundary) => void) {}

  private report(reason: DocumentPreviewPageBoundary['preview_boundary_reason'], spatial?: SpatialChange, missingElapsed = 0): void {
    const bound = (value: number, max: number) => Number.isFinite(value) ? Math.max(0, Math.min(max, value)) : 0
    const decision: DocumentPreviewPageBoundary = { preview_boundary_reason: reason,
      missing_elapsed_ms: bound(missingElapsed, 300_000),
      ...(spatial ? { strong_cells: bound(spatial.strong_cells, 48), quadrants: bound(spatial.quadrants, 4),
        mean_residual: bound(spatial.mean_residual, 255), exposure_offset_abs: bound(spatial.exposure_offset_abs, 255) } : {}) }
    try { this.onBoundary?.(decision) }
    catch { /* Optional diagnostics cannot affect token ownership or sampling. */ }
  }

  reset(): void {
    if (this.pageToken) this.nextCreationReason = 'explicit_invalidation'
    this.source = undefined
    this.frameToken = undefined
    this.pageToken = undefined
    this.previous = undefined
    this.missingSince = undefined
    this.missingBoundary = false
  }

  observe(source: string, frameToken: number, at: number, frame: DocumentFrameFeatures, pageChanged = false): string {
    const newSource = source !== this.source || this.frameToken !== undefined && frameToken < this.frameToken
    const sourceReason = this.nextCreationReason ?? (this.source === undefined ? 'initial' : source !== this.source ? 'source_changed' : 'counter_reset')
    if (newSource) this.reset()
    if (!this.pageToken) {
      this.pageToken = this.newToken()
      this.report(sourceReason)
      this.nextCreationReason = undefined
    }
    if (this.source === source && this.frameToken === frameToken) return this.pageToken
    const missing = frame.paperRatio < .1 || frame.brightness < 28
    if (missing) this.missingSince ??= at
    else { this.missingSince = undefined; this.missingBoundary = false }
    const missingBoundary = this.missingSince !== undefined && at - this.missingSince >= 1_000 && !this.missingBoundary
    // Spatial changes can isolate a page before any formal capture. Warm paper
    // and incomplete framing cannot establish or rule out a page boundary.
    const spatial = !newSource && this.previous ? broadSpatialChange(frame.fingerprint, this.previous.fingerprint) : undefined
    const broadChange = spatial?.changed
    if (!newSource && (pageChanged || missingBoundary || broadChange)) {
      this.pageToken = this.newToken()
      // Match the existing OR-branch priority when multiple signals coincide.
      this.report(pageChanged ? 'page_changed_signal' : missingBoundary ? 'blackout_or_page_missing' : 'broad_spatial_change',
        spatial, this.missingSince === undefined ? 0 : at - this.missingSince)
    }
    if (missingBoundary) this.missingBoundary = true
    this.source = source
    this.frameToken = frameToken
    this.previous = { ...frame, fingerprint: frame.fingerprint.slice() }
    return this.pageToken
  }
}

function broadSpatialChange(a: Uint8Array, b: Uint8Array): SpatialChange | undefined {
  const rows = 6, columns = 8, cells = rows * columns
  if (a.length !== cells || b.length !== cells) return
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
  return { changed: strongCells >= Math.ceil(cells * .35) && quadrants.size >= 3 && totalResidual / cells >= 18,
    strong_cells: strongCells, quadrants: quadrants.size, mean_residual: totalResidual / cells, exposure_offset_abs: Math.abs(exposureShift) }
}
