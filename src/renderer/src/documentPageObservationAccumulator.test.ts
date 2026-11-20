import { describe, expect, it } from 'vitest'
import {
  DocumentPageObservationAccumulator,
  type DocumentObservationEpoch,
  type VerifiedDocumentRegion,
} from './documentPageObservationAccumulator'

const scope = { childId: 'child-a', sessionId: 'lesson-a', deviceLeaseId: 'camera-1', monitorGeneration: 0 }
const left = { x: 0, y: 0, width: 0.5, height: 0.5 }
const right = { x: 0.5, y: 0, width: 0.5, height: 0.5 }
const hash = (token: number) => token.toString(16).padStart(64, '0')
const source = (token: number) => ({
  decodedFrameToken: token, capturedAt: token * 1_000, originalFrameSha256: hash(token),
})

function reading(regionId: string, rect: typeof left, text: string): VerifiedDocumentRegion {
  return { regionId, rect, kind: 'reading', text, visibilityBasis: 'independent_clear_check' }
}

function openFrame(book: DocumentPageObservationAccumulator, epoch: DocumentObservationEpoch, token: number) {
  const frame = source(token)
  return book.beginFrame(epoch, frame)
}

describe('document page original-frame evidence', () => {
  it('makes separate observations viewable with original-frame provenance and different freshness', () => {
    const book = new DocumentPageObservationAccumulator(), epoch = book.openPage(scope, 'page-1')
    book.commitFrame(openFrame(book, epoch, 1), { originalFrameSha256: hash(1), regions: [reading('a', left, '2')] })
    const view = book.commitFrame(openFrame(book, epoch, 2), {
      originalFrameSha256: hash(2), regions: [reading('b', right, '8')],
    })

    expect(view).toMatchObject({
      pageEpoch: 'page-1', scope, requiresReview: true, mayUpdateLearningRecord: false, mayPublish: false,
      regions: [
        { regionId: 'a', kind: 'reading', text: '2', freshness: 'historical_unconfirmed', frame: source(1) },
        { regionId: 'b', kind: 'reading', text: '8', freshness: 'current', frame: source(2) },
      ],
    })
    expect(view).not.toHaveProperty('image')
  })

  it('rejects async results and new requests from a page that closed before OCR returned', () => {
    const book = new DocumentPageObservationAccumulator()
    const first = book.openPage(scope, 'page-1'), oldTicket = openFrame(book, first, 5)
    const second = book.openPage(scope, 'page-2'), newTicket = openFrame(book, second, 6)

    expect(() => book.commitFrame(oldTicket, { originalFrameSha256: hash(5), regions: [reading('a', left, 'old')] }))
      .toThrow('DOCUMENT_OBSERVATION_REPLY_STALE')
    expect(() => openFrame(book, first, 7)).toThrow('DOCUMENT_OBSERVATION_EPOCH_STALE')
    expect(() => openFrame(book, second, 5)).toThrow('DOCUMENT_OBSERVATION_FRAME_STALE')
    expect(book.commitFrame(newTicket, { originalFrameSha256: hash(6), regions: [reading('a', left, 'new')] }))
      .toMatchObject({ pageEpoch: 'page-2', regions: [{ text: 'new', freshness: 'current' }] })
  })

  it('revokes prior capture on stream changes and rejects an equal-looking ticket from another lease', () => {
    const book = new DocumentPageObservationAccumulator()
    const oldEpoch = book.openPage(scope, 'page-1'), oldTicket = openFrame(book, oldEpoch, 10)
    const restarted = book.openPage({ ...scope, deviceLeaseId: 'camera-2', monitorGeneration: 1 }, 'page-1')
    const currentTicket = openFrame(book, restarted, 1)
    expect(() => book.commitFrame(oldTicket, { originalFrameSha256: hash(10), regions: [] }))
      .toThrow('DOCUMENT_OBSERVATION_REPLY_STALE')
    expect(() => book.commitFrame({ ...currentTicket }, { originalFrameSha256: hash(1), regions: [] }))
      .toThrow('DOCUMENT_OBSERVATION_REPLY_STALE')
    expect(book.commitFrame(currentTicket, { originalFrameSha256: hash(1), regions: [] }))
      .toMatchObject({ scope: { deviceLeaseId: 'camera-2', monitorGeneration: 1 }, regions: [] })
    const outstanding = openFrame(book, restarted, 2)
    book.reset()
    expect(book.snapshot()).toBeNull()
    expect(() => book.commitFrame(outstanding, { originalFrameSha256: hash(2), regions: [] }))
      .toThrow('DOCUMENT_OBSERVATION_REPLY_STALE')
  })

  it('keeps a missed reading as history, then updates it only upon verified change or erasure', () => {
    const book = new DocumentPageObservationAccumulator(), epoch = book.openPage(scope, 'page-1')
    book.commitFrame(openFrame(book, epoch, 1), { originalFrameSha256: hash(1), regions: [reading('answer', left, '2')] })
    const blankOcrTicket = openFrame(book, epoch, 2)
    expect(book.snapshot()?.regions).toMatchObject([{ text: '2', freshness: 'historical_unconfirmed' }])
    expect(book.commitFrame(blankOcrTicket, { originalFrameSha256: hash(2), regions: [] }).regions)
      .toMatchObject([{ text: '2', freshness: 'historical_unconfirmed', frame: source(1) }])
    expect(book.commitFrame(openFrame(book, epoch, 3), {
      originalFrameSha256: hash(3), regions: [reading('answer', left, '7')],
    }).regions).toMatchObject([{ text: '7', freshness: 'current', frame: source(3) }])
    const verifiedBlank = book.commitFrame(openFrame(book, epoch, 4), {
      originalFrameSha256: hash(4),
      regions: [{
        regionId: 'answer', rect: left, visibilityBasis: 'human_confirmed',
        kind: 'verified_empty', basis: 'human_confirmed',
      }],
    })
    expect(verifiedBlank.regions).toMatchObject([{ kind: 'verified_empty', freshness: 'current', frame: source(4) }])
    expect(verifiedBlank.regions[0]).not.toHaveProperty('text')
  })

  it('rejects superseded or replayed tickets without overwriting the newer evidence', () => {
    const book = new DocumentPageObservationAccumulator(), epoch = book.openPage(scope, 'page-1')
    const old = openFrame(book, epoch, 1), current = openFrame(book, epoch, 2)
    const result = { originalFrameSha256: hash(2), regions: [reading('answer', left, '7')] }
    const view = book.commitFrame(current, result)
    expect(() => book.commitFrame(old, { originalFrameSha256: hash(1), regions: [reading('answer', left, '2')] }))
      .toThrow('DOCUMENT_OBSERVATION_REPLY_STALE')
    expect(() => book.commitFrame(current, result)).toThrow('DOCUMENT_OBSERVATION_REPLY_STALE')
    expect(book.snapshot()).toEqual(view)
  })

  it('rejects a mismatched source or a conflicting page region atomically', () => {
    const book = new DocumentPageObservationAccumulator(), epoch = book.openPage(scope, 'page-1')
    book.commitFrame(openFrame(book, epoch, 1), { originalFrameSha256: hash(1), regions: [reading('answer', left, '2')] })

    const wrongSource = openFrame(book, epoch, 2)
    expect(() => book.commitFrame(wrongSource, { originalFrameSha256: hash(1), regions: [reading('answer', left, '9')] }))
      .toThrow('DOCUMENT_OBSERVATION_EVIDENCE_INVALID')
    expect(() => book.commitFrame(openFrame(book, epoch, 3), {
      originalFrameSha256: hash(3),
      regions: [reading('answer', left, '7'), reading('unverified-overlap', { ...right, x: 0.4 }, '9')],
    })).toThrow('DOCUMENT_OBSERVATION_REGION_CONFLICT')
    expect(book.snapshot()?.regions).toMatchObject([{ kind: 'reading', text: '2', freshness: 'historical_unconfirmed' }])
  })

  it('bounds retained regions without silently evicting validated observations', () => {
    const book = new DocumentPageObservationAccumulator(), epoch = book.openPage(scope, 'page-1')
    const region = (index: number) =>
      reading(String(index), { x: index / 512, y: 0, width: 1 / 512, height: 1 }, String(index))
    for (let batch = 0; batch < 4; batch++) {
      const token = batch + 1
      book.commitFrame(openFrame(book, epoch, token), {
        originalFrameSha256: hash(token),
        regions: Array.from({ length: 64 }, (_, index) => region(batch * 64 + index)),
      })
    }
    expect(() => book.commitFrame(openFrame(book, epoch, 5), { originalFrameSha256: hash(5), regions: [region(256)] }))
      .toThrow('DOCUMENT_OBSERVATION_REGION_LIMIT')
    const regions = book.snapshot()?.regions ?? []
    expect(regions).toHaveLength(256)
    expect(regions[0]).toMatchObject({ regionId: '0', freshness: 'historical_unconfirmed' })
    expect(regions[255]).toMatchObject({ regionId: '255', freshness: 'historical_unconfirmed' })
  })
})
