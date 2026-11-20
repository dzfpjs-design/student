import type { PageRect } from './documentPageCandidates'

export type DocumentObservationScope = Readonly<{
  childId: string
  sessionId: string
  deviceLeaseId: string
  monitorGeneration: number
}>

export type DocumentObservationEpoch = Readonly<{
  scope: DocumentObservationScope
  pageEpoch: string
}>

export type DocumentOriginalFrame = Readonly<{
  decodedFrameToken: number
  capturedAt: number
  originalFrameSha256: string
}>

export type DocumentObservationTicket = Readonly<{
  epoch: DocumentObservationEpoch
  frame: DocumentOriginalFrame
}>

/** Rectangles are in registered page coordinates. Establish visibility
 * independently before submitting a line or empty region; an OCR box, a skin
 * colour estimate, or the absence of OCR text does not establish it. */
export type VerifiedDocumentRegion = Readonly<{
  regionId: string
  rect: Readonly<PageRect>
  visibilityBasis: 'independent_clear_check' | 'human_confirmed'
}> & (
  | Readonly<{ kind: 'reading'; text: string; confidence?: number }>
  | Readonly<{ kind: 'verified_empty'; basis: 'independent_blank_check' | 'human_confirmed' }>
)

export type VerifiedDocumentFrameResult = Readonly<{
  originalFrameSha256: string
  regions: readonly VerifiedDocumentRegion[]
}>

export type DocumentPageObservationRegion = VerifiedDocumentRegion & Readonly<{
  frame: DocumentOriginalFrame
  freshness: 'current' | 'historical_unconfirmed'
}>

export type DocumentPageObservationView = Readonly<{
  scope: DocumentObservationScope
  pageEpoch: string
  latestFrame: DocumentOriginalFrame | null
  regions: readonly DocumentPageObservationRegion[]
  requiresReview: true
  mayUpdateLearningRecord: false
  mayPublish: false
}>

type RegionEvidence = VerifiedDocumentRegion & { frame: DocumentOriginalFrame }

const MAX_REGIONS_PER_FRAME = 64
const MAX_PAGE_REGIONS = 256
const MAX_TEXT_LENGTH = 2_000
const SHA256 = /^[a-f0-9]{64}$/

function validId(value: string): boolean {
  return typeof value === 'string' && value.length > 0 && value.length <= 128 && value.trim() === value
}

function sameStream(a: DocumentObservationScope, b: DocumentObservationScope): boolean {
  return a.childId === b.childId && a.sessionId === b.sessionId &&
    a.deviceLeaseId === b.deviceLeaseId && a.monitorGeneration === b.monitorGeneration
}

function validRect(rect: Readonly<PageRect>): boolean {
  return !!rect && [rect.x, rect.y, rect.width, rect.height].every(Number.isFinite) &&
    rect.x >= 0 && rect.y >= 0 && rect.width > 0 && rect.height > 0 &&
    rect.x + rect.width <= 1 && rect.y + rect.height <= 1
}

function sameRect(a: Readonly<PageRect>, b: Readonly<PageRect>): boolean {
  return a.x === b.x && a.y === b.y && a.width === b.width && a.height === b.height
}

function overlaps(a: Readonly<PageRect>, b: Readonly<PageRect>): boolean {
  return Math.max(a.x, b.x) < Math.min(a.x + a.width, b.x + b.width) &&
    Math.max(a.y, b.y) < Math.min(a.y + a.height, b.y + b.height)
}

function copyRegion(region: VerifiedDocumentRegion): VerifiedDocumentRegion {
  const { regionId, visibilityBasis } = region
  const { x, y, width, height } = region.rect
  const base = { regionId, visibilityBasis, rect: { x, y, width, height } }
  if (region.kind === 'verified_empty') return { ...base, kind: 'verified_empty', basis: region.basis }
  return { ...base, kind: 'reading', text: region.text.trim(), confidence: region.confidence }
}

function validRegion(region: VerifiedDocumentRegion): boolean {
  if (!region || !validId(region.regionId) || !validRect(region.rect) ||
      (region.visibilityBasis !== 'independent_clear_check' && region.visibilityBasis !== 'human_confirmed')) return false
  if (region.kind === 'verified_empty')
    return region.basis === 'independent_blank_check' || region.basis === 'human_confirmed'
  if (region.kind !== 'reading') return false
  return typeof region.text === 'string' && region.text.length <= MAX_TEXT_LENGTH && !!region.text.trim() &&
    (region.confidence === undefined ||
      (Number.isFinite(region.confidence) && region.confidence >= 0 && region.confidence <= 1))
}

/** In-memory observations from bound original frames, with no image data,
 * persistence, or publishing. The caller verifies page identity, registers
 * regions, and binds the OCR response to the exact original frame. */
export class DocumentPageObservationAccumulator {
  private epoch: DocumentObservationEpoch | null = null
  private pending: DocumentObservationTicket | null = null
  private latestFrame: DocumentOriginalFrame | null = null
  private lastStreamFrame: DocumentOriginalFrame | null = null
  private regions = new Map<string, RegionEvidence>()

  /** Call only after establishing the new page; an OCR reply cannot open it.
   * Opening a page replaces its lease even when the supplied ID is reused. */
  openPage(scope: DocumentObservationScope, pageEpoch: string): DocumentObservationEpoch {
    if (!scope || !validId(scope.childId) || !validId(scope.sessionId) || !validId(scope.deviceLeaseId) ||
        !Number.isSafeInteger(scope.monitorGeneration) || scope.monitorGeneration < 0 || !validId(pageEpoch))
      throw new Error('DOCUMENT_OBSERVATION_SCOPE_INVALID')
    if (!this.epoch || !sameStream(this.epoch.scope, scope)) this.lastStreamFrame = null
    const { childId, sessionId, deviceLeaseId, monitorGeneration } = scope
    this.epoch = Object.freeze({
      scope: Object.freeze({ childId, sessionId, deviceLeaseId, monitorGeneration }), pageEpoch,
    })
    this.pending = null
    this.latestFrame = null
    this.regions.clear()
    return this.epoch
  }

  /** A newer capture supersedes any outstanding reply. An outstanding frame
   * without verified regions makes earlier observations historical. */
  beginFrame(epoch: DocumentObservationEpoch, input: DocumentOriginalFrame): DocumentObservationTicket {
    if (this.epoch !== epoch || !epoch) throw new Error('DOCUMENT_OBSERVATION_EPOCH_STALE')
    if (!input || !Number.isSafeInteger(input.decodedFrameToken) || input.decodedFrameToken < 0 ||
        !Number.isFinite(input.capturedAt) || input.capturedAt < 0 ||
        typeof input.originalFrameSha256 !== 'string' || !SHA256.test(input.originalFrameSha256))
      throw new Error('DOCUMENT_OBSERVATION_FRAME_INVALID')
    if (this.lastStreamFrame && (input.decodedFrameToken <= this.lastStreamFrame.decodedFrameToken ||
        input.capturedAt <= this.lastStreamFrame.capturedAt))
      throw new Error('DOCUMENT_OBSERVATION_FRAME_STALE')
    const { decodedFrameToken, capturedAt, originalFrameSha256 } = input
    this.latestFrame = Object.freeze({ decodedFrameToken, capturedAt, originalFrameSha256 })
    this.lastStreamFrame = this.latestFrame
    return this.pending = Object.freeze({ epoch, frame: this.latestFrame })
  }

  commitFrame(ticket: DocumentObservationTicket, result: VerifiedDocumentFrameResult): DocumentPageObservationView {
    const epoch = this.epoch
    if (!epoch || !ticket || this.pending !== ticket || epoch !== ticket.epoch)
      throw new Error('DOCUMENT_OBSERVATION_REPLY_STALE')
    this.pending = null
    if (!result || result.originalFrameSha256 !== ticket.frame.originalFrameSha256 ||
        !Array.isArray(result.regions) || result.regions.length > MAX_REGIONS_PER_FRAME)
      throw new Error('DOCUMENT_OBSERVATION_EVIDENCE_INVALID')

    const next = new Map(this.regions)
    const observedIds = new Set<string>()
    for (const observation of result.regions) {
      if (!validRegion(observation) || observedIds.has(observation.regionId))
        throw new Error('DOCUMENT_OBSERVATION_EVIDENCE_INVALID')
      const { regionId, rect } = observation
      const previous = next.get(regionId)
      if (previous ? !sameRect(rect, previous.rect) :
          Array.from(next.values()).some((region) => overlaps(rect, region.rect)))
        throw new Error('DOCUMENT_OBSERVATION_REGION_CONFLICT')
      next.set(regionId, { ...copyRegion(observation), frame: ticket.frame })
      observedIds.add(regionId)
    }
    if (next.size > MAX_PAGE_REGIONS) throw new Error('DOCUMENT_OBSERVATION_REGION_LIMIT')
    this.regions = next
    return this.view(epoch)
  }

  snapshot(): DocumentPageObservationView | null {
    return this.epoch ? this.view(this.epoch) : null
  }

  /** Revoke in-flight results on a disconnect, scope change, or teardown. */
  reset(): void {
    this.epoch = null
    this.pending = null
    this.latestFrame = null
    this.lastStreamFrame = null
    this.regions.clear()
  }

  private view(epoch: DocumentObservationEpoch): DocumentPageObservationView {
    const latestFrame = this.latestFrame
    return {
      scope: epoch.scope,
      pageEpoch: epoch.pageEpoch,
      latestFrame,
      regions: Array.from(this.regions.values(), (region) => ({
        ...copyRegion(region),
        frame: region.frame,
        freshness: region.frame === latestFrame ? 'current' : 'historical_unconfirmed',
      })),
      requiresReview: true,
      mayUpdateLearningRecord: false,
      mayPublish: false,
    }
  }
}
