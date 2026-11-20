import { readDocumentOcrFrameGeometry } from './documentOcrFrameGeometry'
import type {
  DocumentObservationTicket, DocumentPageObservationAccumulator, DocumentPageObservationView,
  VerifiedDocumentRegion,
} from './documentPageObservationAccumulator'
import type { PageRect } from './documentPageCandidates'

export type VisibleDocumentRegion = {
  regionId: string
  frameRect: Readonly<PageRect>
  pageRect: Readonly<PageRect>
  visibilityBasis: VerifiedDocumentRegion['visibilityBasis']
}

function contains(outer: Readonly<PageRect>, inner: Readonly<PageRect>): boolean {
  return inner.x >= outer.x && inner.y >= outer.y &&
    inner.x + inner.width <= outer.x + outer.width && inner.y + inner.height <= outer.y + outer.height
}

function normalized(rect: Readonly<PageRect>): boolean {
  return [rect.x, rect.y, rect.width, rect.height].every(Number.isFinite) &&
    rect.x >= 0 && rect.y >= 0 && rect.width > 0 && rect.height > 0 &&
    rect.x + rect.width <= 1 && rect.y + rect.height <= 1
}

function overlaps(a: Readonly<PageRect>, b: Readonly<PageRect>): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width &&
    a.y < b.y + b.height && b.y < a.y + a.height
}

/**
 * Read original-frame OCR only within regions whose visibility and registration
 * have already been verified. The caller must obtain this information separately
 * from the OCR response. Missing text does not imply erasure.
 */
export async function commitDocumentOriginalFrame(
  observations: DocumentPageObservationAccumulator,
  ticket: DocumentObservationTicket,
  source: { jpeg: Uint8Array; width: number; height: number },
  artifact: unknown,
  visibility: { ticket: DocumentObservationTicket; regions: readonly VisibleDocumentRegion[] },
): Promise<DocumentPageObservationView> {
  if (visibility.ticket !== ticket) throw new Error('DOCUMENT_ORIGINAL_FRAME_VISIBILITY_STALE')
  const scope = ticket.epoch.scope
  const response = artifact as {
    child_id?: string; session_id?: string; details?: { monitor_generation?: number }
  } | null
  if (response?.child_id !== scope.childId || response.session_id !== scope.sessionId ||
      response.details?.monitor_generation !== scope.monitorGeneration) {
    throw new Error('DOCUMENT_ORIGINAL_FRAME_SCOPE_MISMATCH')
  }
  if (source.jpeg.byteLength === 0 || source.jpeg.byteLength > 18_000_000 || visibility.regions.length > 64) {
    throw new Error('DOCUMENT_ORIGINAL_FRAME_INVALID')
  }
  const regions = visibility.regions.map(region => ({
    ...region, frameRect: { ...region.frameRect }, pageRect: { ...region.pageRect },
  }))
  if (regions.some((region, index) => !normalized(region.frameRect) || !normalized(region.pageRect) ||
      regions.slice(0, index).some(previous => previous.regionId === region.regionId ||
        overlaps(previous.frameRect, region.frameRect) || overlaps(previous.pageRect, region.pageRect)))) {
    throw new Error('DOCUMENT_ORIGINAL_FRAME_REGION_INVALID')
  }
  const width = source.width
  const height = source.height
  const digest = await crypto.subtle.digest('SHA-256', Uint8Array.from(source.jpeg))
  const sha256 = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
  if (sha256 !== ticket.frame.originalFrameSha256) throw new Error('DOCUMENT_ORIGINAL_FRAME_MISMATCH')

  const lines = await readDocumentOcrFrameGeometry(artifact, { sha256, width, height })
  const readings = new Map<VisibleDocumentRegion, typeof lines[number][]>()
  for (const line of lines) {
    const matches = regions.filter(region => contains(region.frameRect, line.box))
    if (matches.length !== 1) continue
    const region = matches[0]
    const group = readings.get(region)
    if (group) group.push(line)
    else readings.set(region, [line])
  }
  const verified: VerifiedDocumentRegion[] = Array.from(readings, ([region, group]) => ({
    regionId: region.regionId,
    rect: region.pageRect,
    visibilityBasis: region.visibilityBasis,
    kind: 'reading',
    text: group.map(line => line.text).join('\n'),
    confidence: Math.min(...group.map(line => line.confidence)),
  }))
  // The accumulator rejects a reply whose ticket was invalidated while the
  // original JPEG and OCR spans were being checked.
  return observations.commitFrame(ticket, { originalFrameSha256: sha256, regions: verified })
}
