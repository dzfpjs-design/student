import { contentChanged, type DocumentFrameFeatures } from './documentFrameMonitor'

export type DocumentPreviewPageBoundary = Readonly<{
  preview_boundary_reason: 'initial' | 'source_changed' | 'counter_reset' | 'explicit_invalidation'
  strong_cells?: number
  quadrants?: number
  mean_residual?: number
  exposure_offset_abs?: number
  missing_elapsed_ms: number
}>
type SpatialChange = { changed: boolean; strong_cells: number; quadrants: number; mean_residual: number; exposure_offset_abs: number }
export type DocumentPreviewSceneChange = Readonly<Omit<DocumentPreviewPageBoundary, 'preview_boundary_reason'> & {
  preview_scene_reason: 'blackout_or_page_missing' | 'page_changed_signal' | 'broad_spatial_change' | 'reference_returned' | 'registered'
  scene_elapsed_ms: number
}>
type PendingScene = { reference?: DocumentFrameFeatures; at: number; changedFrame: number;
  returnFirstAt?: number; returnLastAt?: number; returnCount: number }

/** A token fences a continuous capture source, never physical paper identity. */
export class DocumentPreviewPageEpoch {
  private source?: string
  private frameToken?: number
  private pageToken?: string
  private previous?: DocumentFrameFeatures
  private missingSince?: number
  private missingBoundary = false
  private missingReference?: DocumentFrameFeatures
  private pendingScene?: PendingScene
  private nextCreationReason?: DocumentPreviewPageBoundary['preview_boundary_reason']

  constructor(private readonly newToken: () => string = () => crypto.randomUUID(),
    private readonly onBoundary?: (decision: DocumentPreviewPageBoundary) => void,
    private readonly onSceneChange?: (decision: DocumentPreviewSceneChange) => void) {}

  private report(reason: DocumentPreviewPageBoundary['preview_boundary_reason'], spatial?: SpatialChange, missingElapsed = 0): void {
    const bound = (value: number, max: number) => Number.isFinite(value) ? Math.max(0, Math.min(max, value)) : 0
    const decision: DocumentPreviewPageBoundary = { preview_boundary_reason: reason,
      missing_elapsed_ms: bound(missingElapsed, 300_000),
      ...(spatial ? { strong_cells: bound(spatial.strong_cells, 48), quadrants: bound(spatial.quadrants, 4),
        mean_residual: bound(spatial.mean_residual, 255), exposure_offset_abs: bound(spatial.exposure_offset_abs, 255) } : {}) }
    try { this.onBoundary?.(decision) }
    catch { /* Optional diagnostics cannot affect token ownership or sampling. */ }
  }

  private reportScene(reason: DocumentPreviewSceneChange['preview_scene_reason'], at: number, spatial?: SpatialChange): void {
    if (!this.pendingScene) return
    const bound = (value: number, max: number) => Number.isFinite(value) ? Math.max(0, Math.min(max, value)) : 0
    const decision: DocumentPreviewSceneChange = { preview_scene_reason: reason,
      scene_elapsed_ms: bound(at - this.pendingScene.at, 300_000),
      missing_elapsed_ms: bound(this.missingSince === undefined ? 0 : at - this.missingSince, 300_000),
      ...(spatial ? { strong_cells: bound(spatial.strong_cells, 48), quadrants: bound(spatial.quadrants, 4),
        mean_residual: bound(spatial.mean_residual, 255), exposure_offset_abs: bound(spatial.exposure_offset_abs, 255) } : {}) }
    try { this.onSceneChange?.(decision) }
    catch { /* Optional diagnostics cannot affect the capture domain. */ }
  }

  acknowledgeRegistered(pageToken: string, frameToken: number, at: number): void {
    // Only the caller's checked ORB + independent-text receipt may take this
    // path. An old domain or a capture predating the last disturbance cannot.
    if (pageToken !== this.pageToken || !Number.isSafeInteger(frameToken) || frameToken < 0
        || !this.pendingScene || frameToken < this.pendingScene.changedFrame
        || this.frameToken === undefined || frameToken > this.frameToken) return
    this.reportScene('registered', at)
    this.pendingScene = undefined
  }

  reset(): void {
    if (this.pageToken) this.nextCreationReason = 'explicit_invalidation'
    this.source = undefined
    this.frameToken = undefined
    this.pageToken = undefined
    this.previous = undefined
    this.missingSince = undefined
    this.missingBoundary = false
    this.missingReference = undefined
    this.pendingScene = undefined
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
    if (missing && this.missingSince === undefined) {
      this.missingSince = at
      this.missingReference = this.previous
    } else if (!missing) {
      this.missingSince = undefined; this.missingBoundary = false; this.missingReference = undefined
    }
    const missingBoundary = this.missingSince !== undefined && at - this.missingSince >= 1_000 && !this.missingBoundary
    // Color, motion and elapsed missing-paper time only suggest a scene
    // change. None can distinguish a hand from a physical page turn.
    const spatial = !newSource && this.previous ? broadSpatialChange(frame.fingerprint, this.previous.fingerprint) : undefined
    const broadChange = spatial?.changed
    if (!newSource && (pageChanged || missingBoundary || broadChange)) {
      if (!this.pendingScene) {
        this.pendingScene = { reference: missingBoundary ? this.missingReference : this.previous,
          at, changedFrame: frameToken, returnCount: 0 }
        this.reportScene(pageChanged ? 'page_changed_signal' : missingBoundary ? 'blackout_or_page_missing' : 'broad_spatial_change', at, spatial)
      } else this.pendingScene.changedFrame = frameToken
    }
    if (missingBoundary) this.missingBoundary = true
    const pending = this.pendingScene
    const reference = pending?.reference
    const returning = reference ? broadSpatialChange(frame.fingerprint, reference.fingerprint) : undefined
    if (pending && reference && !missing && frame.contrast >= 7 && returning && returning.mean_residual <= 4.5
        && reference.contentFingerprint && frame.contentFingerprint
        && !contentChanged(reference.contentFingerprint, frame.contentFingerprint)) {
      const continuous = pending.returnLastAt !== undefined && at > pending.returnLastAt && at - pending.returnLastAt <= 1_250
      pending.returnFirstAt = continuous ? pending.returnFirstAt : at
      pending.returnLastAt = at
      pending.returnCount = continuous ? pending.returnCount + 1 : 1
      if (pending.returnCount >= 4 && pending.returnFirstAt !== undefined && at - pending.returnFirstAt >= 1_500) {
        this.reportScene('reference_returned', at, returning)
        this.pendingScene = undefined
      }
    } else if (pending) {
      pending.returnFirstAt = undefined; pending.returnLastAt = undefined; pending.returnCount = 0
    }
    this.source = source
    this.frameToken = frameToken
    this.previous = { ...frame, fingerprint: frame.fingerprint.slice(), contentFingerprint: frame.contentFingerprint
      ? { ...frame.contentFingerprint, pixels: frame.contentFingerprint.pixels.slice() } : undefined }
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
  // This is only a scene-change hint. It must never establish or cut a
  // physical page identity, including when a large hand meets every threshold.
  return { changed: strongCells >= Math.ceil(cells * .35) && quadrants.size >= 3 && totalResidual / cells >= 18,
    strong_cells: strongCells, quadrants: quadrants.size, mean_residual: totalResidual / cells, exposure_offset_abs: Math.abs(exposureShift) }
}
