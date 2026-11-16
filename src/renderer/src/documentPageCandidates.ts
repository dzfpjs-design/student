/** Provisional, in-memory transcription fragments from one locally identified page.
 *
 * This is not a page-identification or hand-detection algorithm. Callers must
 * provide a locally verified page epoch and regions that are actually visible.
 * Fragments retained through occlusion can be out of date, so this structure
 * never grants authority for grading, automatic mistake capture, or a report.
 */
export type PageRect = { x: number; y: number; width: number; height: number }
export type PageTextFragment = { text: string; rect: PageRect }
export type PageFrameObservation = {
  childId: string
  sessionId: string
  monitorGeneration: number
  pageEpoch: string
  frameId: string
  observedAt: number
  /** Disjoint or overlapping areas independently judged visible in this frame. */
  visible: PageRect[]
  /** Regions independently verified empty; an OCR miss alone is not erasure evidence. */
  verifiedEmpty?: PageRect[]
  fragments: PageTextFragment[]
}
export type ProvisionalPageFragment = PageTextFragment & {
  frameId: string
  observedAt: number
  freshness: 'current' | 'historical_unconfirmed'
}
export type ProvisionalPage = {
  pageEpoch: string
  frameId: string
  fragments: ProvisionalPageFragment[]
  reviewRequired: true
  mayUpdateLearningRecord: false
}

const validRect = (r: PageRect): boolean => !!r && [r.x, r.y, r.width, r.height].every(Number.isFinite)
  && r.x >= 0 && r.y >= 0 && r.width > 0 && r.height > 0 && r.x + r.width <= 1 && r.y + r.height <= 1

function inside(inner: PageRect, outer: PageRect): boolean {
  return inner.x >= outer.x && inner.y >= outer.y
    && inner.x + inner.width <= outer.x + outer.width && inner.y + inner.height <= outer.y + outer.height
}

function overlap(a: PageRect, b: PageRect): number {
  const w = Math.max(0, Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x))
  const h = Math.max(0, Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y))
  const area = w * h
  return area / Math.max(a.width * a.height, b.width * b.height)
}

export class DocumentPageCandidates {
  private epoch: string | null = null
  private lastAt = -1
  private readonly seenFrameIds = new Set<string>()
  private fragments: ProvisionalPageFragment[] = []
  private invalidated = false
  private readonly scope: { childId: string; sessionId: string; monitorGeneration: number }

  constructor(scope: { childId: string; sessionId: string; monitorGeneration: number }) {
    if (!scope.childId || !scope.sessionId || !Number.isSafeInteger(scope.monitorGeneration) || scope.monitorGeneration < 0) {
      throw new Error('DOCUMENT_PAGE_SCOPE_INVALID')
    }
    this.scope = { ...scope }
  }

  clear(): void {
    this.epoch = null
    this.lastAt = -1
    this.seenFrameIds.clear()
    this.fragments = []
  }

  observe(frame: PageFrameObservation): ProvisionalPage {
    if (this.invalidated) throw new Error('DOCUMENT_PAGE_SCOPE_CHANGED')
    if (frame.childId !== this.scope.childId || frame.sessionId !== this.scope.sessionId
        || frame.monitorGeneration !== this.scope.monitorGeneration) {
      this.clear()
      this.invalidated = true
      throw new Error('DOCUMENT_PAGE_SCOPE_CHANGED')
    }
    if (!frame.pageEpoch || !frame.frameId || !Number.isFinite(frame.observedAt)
        || frame.observedAt < 0 || !Array.isArray(frame.visible) || !Array.isArray(frame.fragments)
        || frame.visible.length > 64 || frame.fragments.length > 200 || (frame.verifiedEmpty?.length ?? 0) > 64
        || !frame.visible.every(validRect)
        || (frame.verifiedEmpty !== undefined && (!Array.isArray(frame.verifiedEmpty)
          || !frame.verifiedEmpty.every(r => validRect(r) && frame.visible.some(v => inside(r, v)))))
        || !frame.fragments.every(f => typeof f.text === 'string' && f.text.trim().length > 0
          && f.text.length <= 2000 && validRect(f.rect) && frame.visible.some(v => inside(f.rect, v)))) {
      throw new Error('DOCUMENT_PAGE_CANDIDATE_INVALID')
    }
    if (frame.observedAt <= this.lastAt || this.seenFrameIds.has(frame.frameId)) throw new Error('DOCUMENT_PAGE_FRAME_STALE')
    if (frame.pageEpoch !== this.epoch) {
      // A late result from an old page must not rewind the active page.
      // Keep the stream's monotonic time and frame IDs across page changes.
      this.epoch = frame.pageEpoch
      this.fragments = []
    }
    // OCR omission does not prove erasure. Only independently verified empty
    // regions may remove prior text; everything else remains unconfirmed.
    this.fragments = this.fragments.filter(f => !(frame.verifiedEmpty ?? []).some(v => inside(f.rect, v)))
      .map(f => ({ ...f, freshness: 'historical_unconfirmed' as const }))
    for (const f of frame.fragments) {
      // A changed line replaces overlapping prior hypotheses; do not concatenate
      // conflicting OCR text as if both had appeared on the current page.
      this.fragments = this.fragments.filter(previous => overlap(previous.rect, f.rect) < .6)
      this.fragments.push({ text: f.text.trim(), rect: { ...f.rect }, frameId: frame.frameId,
        observedAt: frame.observedAt, freshness: 'current' })
    }
    if (this.fragments.length > 400) { this.clear(); throw new Error('DOCUMENT_PAGE_CANDIDATE_LIMIT') }
    this.lastAt = frame.observedAt
    this.seenFrameIds.add(frame.frameId)
    if (this.seenFrameIds.size > 512) { this.clear(); throw new Error('DOCUMENT_PAGE_CANDIDATE_LIMIT') }
    return { pageEpoch: frame.pageEpoch, frameId: frame.frameId,
      fragments: this.fragments.map(f => ({ ...f, rect: { ...f.rect } })),
      reviewRequired: true, mayUpdateLearningRecord: false }
  }
}
