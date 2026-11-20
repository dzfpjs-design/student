// @vitest-environment node
import { createHash } from 'node:crypto'
import { describe, expect, it, vi } from 'vitest'
import { readDocumentOcrFrameGeometry } from './documentOcrFrameGeometry'
import { commitDocumentOriginalFrame } from './commitDocumentOriginalFrame'
import { DocumentPageObservationAccumulator } from './documentPageObservationAccumulator'

const sha = (text: string): string => createHash('sha256').update(text).digest('hex')
const source = { sha256: sha('synthetic original JPEG bytes'), width: 1200, height: 1600 }
const quad = [[120, 320], [360, 320], [360, 360], [120, 360]]

function artifactFixture(lines = [
  { text: '计划𠀀一', confidence: .96, frame: quad },
  { text: '第二行', confidence: .91, frame: [[120, 400], [400, 400], [400, 440], [120, 440]] }
], originalSha = source.sha256) {
  let offset = 0
  const extractedText = lines.map(line => line.text).join('\n')
  const records = lines.map((line, index) => {
    const start = offset
    const end = start + Array.from(line.text).length
    offset = end + 1
    return {
      line_index: index, start, end, text_sha256: sha(line.text),
      frame_polygon: line.frame.map(point => [...point]),
      ocr_polygon: line.frame.map(([x, y]) => [source.height - 1 - y, x])
    }
  })
  return {
    artifact_type: 'document_ocr',
    status: 'quality_warning',
    child_id: 'test-child',
    session_id: 'test-session',
    extracted_text: extractedText,
    details: {
      engine: 'rapidocr-onnx',
      monitor_generation: 1,
      monitor_capture_kind: 'single_frame',
      monitor_frame_sha256: originalSha,
      lines: lines.map((line, i) => ({
        text: line.text,
        confidence: line.confidence,
        box: records[i].ocr_polygon.map(point => [...point])
      })),
      local_pipeline: {
        engine: 'rapidocr-onnx',
        image_sha256: originalSha,
        privacy: { raw_image_uploaded: false },
        quality: { orientation_evidence: { ambiguous: false } },
        text_geometry: {
          version: 'ocr_text_geometry_v1',
          frame_sha256: originalSha,
          frame_size: [source.width, source.height],
          prepared_size: [source.width, source.height],
          orientation_degrees: 90,
          frame_to_ocr: [[0, -1, source.height - 1], [1, 0, 0], [0, 0, 1]],
          text_sha256: sha(extractedText),
          geometry_sha256: sha('synthetic geometry'),
          lines: records
        }
      },
      cloud_vision_fallback: { text: '不应进入本地观察' }
    }
  }
}

describe('original-frame OCR geometry', () => {
  it('keeps local positive readings in original camera coordinates after rotation', async () => {
    const found = await readDocumentOcrFrameGeometry(artifactFixture(), source)
    expect(found).toEqual([
      { text: '计划𠀀一', confidence: .96, box: { x: .1, y: .2, width: .2, height: .025 } },
      { text: '第二行', confidence: .91, box: { x: .1, y: .25, width: 280 / 1200, height: .025 } }
    ])
  })

  it('fails closed for different original bytes, declared size or digest syntax', async () => {
    const artifact = artifactFixture()
    await expect(readDocumentOcrFrameGeometry(artifact, { ...source, sha256: sha('other synthetic JPEG') })).resolves.toEqual([])
    await expect(readDocumentOcrFrameGeometry(artifact, { ...source, sha256: '0'.repeat(63) })).resolves.toEqual([])
    await expect(readDocumentOcrFrameGeometry(artifact, { ...source, width: source.width + 1 })).resolves.toEqual([])
  })

  it.each<[(artifact: ReturnType<typeof artifactFixture>) => void]>([
    [artifact => { artifact.artifact_type = 'document_ocr_candidate' }],
    [artifact => { artifact.details.monitor_capture_kind = 'multiframe_candidate' }],
    [artifact => { artifact.details.monitor_frame_sha256 = sha('another frame') }],
    [artifact => { artifact.details.local_pipeline.image_sha256 = sha('another frame') }],
    [artifact => { artifact.details.local_pipeline.engine = 'cloud-text' }],
    [artifact => { artifact.details.local_pipeline.quality.orientation_evidence.ambiguous = true }],
    [artifact => { artifact.details.local_pipeline.text_geometry.lines[1].start += 1 }],
    [artifact => { artifact.details.local_pipeline.text_geometry.lines[1].text_sha256 = sha('other line') }],
    [artifact => { artifact.extracted_text += '\n伪造内容' }]
  ])('rejects invalid local source or span evidence %#', async mutate => {
    const artifact = artifactFixture()
    mutate(artifact)
    await expect(readDocumentOcrFrameGeometry(artifact, source)).resolves.toEqual([])
  })

  it('excludes uncertain, non-finite, crossing, collapsed and out-of-frame lines', async () => {
    const artifact = artifactFixture([
      { text: '可用', confidence: .96, frame: quad },
      { text: '低可信', confidence: .899, frame: quad },
      { text: '边界外', confidence: .96, frame: [[-5, 320], [360, 320], [360, 360], [-5, 360]] },
      { text: '交叉', confidence: .96, frame: [quad[0], quad[2], quad[1], quad[3]] },
      { text: '退化', confidence: .96, frame: [[120, 320], [121, 320], [121, 321], [120, 321]] },
      { text: '点异常', confidence: .96, frame: quad },
      { text: '坐标不符', confidence: .96, frame: quad }
    ])
    artifact.details.local_pipeline.text_geometry.lines[5].frame_polygon[0][0] = Number.NaN
    artifact.details.local_pipeline.text_geometry.lines[6].frame_polygon[0][0] += 10
    const observed = await readDocumentOcrFrameGeometry(artifact, source)
    expect(observed).toEqual([{ text: '可用', confidence: .96, box: { x: .1, y: .2, width: .2, height: .025 } }])
  })

  it('does not return unverified text when digest verification is unavailable', async () => {
    vi.stubGlobal('crypto', {})
    try {
      await expect(readDocumentOcrFrameGeometry(artifactFixture(), source)).resolves.toEqual([])
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

describe('bounded proof from separate original frames', () => {
  const scope = { childId: 'test-child', sessionId: 'test-session', deviceLeaseId: 'test-camera', monitorGeneration: 1 }
  const jpeg = (label: string) => ({ width: source.width, height: source.height, jpeg: new TextEncoder().encode(label) })
  const upper = { x: .05, y: .18, width: .5, height: .06 }
  const lower = { x: .05, y: .245, width: .5, height: .06 }
  const visible = (regionId: string, frameRect: typeof upper) => ({
    regionId, frameRect, pageRect: frameRect, visibilityBasis: 'independent_clear_check' as const,
  })

  it('retains earlier text as unconfirmed when a distinct original frame verifies another region', async () => {
    const observations = new DocumentPageObservationAccumulator()
    const page = observations.openPage(scope, 'same-page')
    const first = 'synthetic original JPEG bytes'
    const firstTicket = observations.beginFrame(page, { decodedFrameToken: 1, capturedAt: 1000, originalFrameSha256: sha(first) })
    await commitDocumentOriginalFrame(observations, firstTicket, jpeg(first), artifactFixture(),
      { ticket: firstTicket, regions: [visible('upper', upper)] })

    const second = 'new original JPEG bytes'
    const secondTicket = observations.beginFrame(page, { decodedFrameToken: 2, capturedAt: 2000, originalFrameSha256: sha(second) })
    const review = await commitDocumentOriginalFrame(observations, secondTicket, jpeg(second),
      artifactFixture(undefined, sha(second)), { ticket: secondTicket, regions: [visible('lower', lower)] })

    expect(review.regions.map(region => [region.regionId, region.freshness, region.frame.originalFrameSha256]))
      .toEqual([['upper', 'historical_unconfirmed', sha(first)], ['lower', 'current', sha(second)]])
    expect(review.mayPublish).toBe(false)
    expect(review.mayUpdateLearningRecord).toBe(false)
  })

  it('rejects substituted bytes and a delayed result from before a page turn', async () => {
    const observations = new DocumentPageObservationAccumulator()
    const page = observations.openPage(scope, 'page-before-turn')
    const first = 'synthetic original JPEG bytes'
    const ticket = observations.beginFrame(page, { decodedFrameToken: 1, capturedAt: 1000, originalFrameSha256: sha(first) })
    await expect(commitDocumentOriginalFrame(observations, ticket, jpeg('different JPEG'),
      artifactFixture(), { ticket, regions: [visible('upper', upper)] })).rejects.toThrow('DOCUMENT_ORIGINAL_FRAME_MISMATCH')

    const pending = commitDocumentOriginalFrame(observations, ticket, jpeg(first),
      artifactFixture(), { ticket, regions: [visible('upper', upper)] })
    observations.openPage(scope, 'page-after-turn')
    await expect(pending).rejects.toThrow('DOCUMENT_OBSERVATION_REPLY_STALE')
    expect(observations.snapshot()).toMatchObject({ pageEpoch: 'page-after-turn', regions: [] })
  })

  it('refuses the same image from a different session, child or generation and rejects stale visibility', async () => {
    const observations = new DocumentPageObservationAccumulator()
    const page = observations.openPage(scope, 'same-page')
    const image = 'synthetic original JPEG bytes'
    const first = observations.beginFrame(page, { decodedFrameToken: 1, capturedAt: 1000, originalFrameSha256: sha(image) })
    const current = observations.beginFrame(page, { decodedFrameToken: 2, capturedAt: 2000, originalFrameSha256: sha(image) })
    const visibility = { ticket: current, regions: [visible('upper', upper)] }
    const artifact = artifactFixture()

    for (const wrongScope of [
      { ...artifact, child_id: 'other-child' },
      { ...artifact, session_id: 'old-session' },
      { ...artifact, details: { ...artifact.details, monitor_generation: 0 } },
    ]) {
      await expect(commitDocumentOriginalFrame(observations, current, jpeg(image), wrongScope, visibility))
        .rejects.toThrow('DOCUMENT_ORIGINAL_FRAME_SCOPE_MISMATCH')
    }
    await expect(commitDocumentOriginalFrame(observations, current, jpeg(image), artifact, { ...visibility, ticket: first }))
      .rejects.toThrow('DOCUMENT_ORIGINAL_FRAME_VISIBILITY_STALE')
    await expect(commitDocumentOriginalFrame(observations, current, jpeg(image), artifact,
      { ticket: current, regions: [visible('upper', upper), visible('overlap', { ...upper, y: .215 })] }))
      .rejects.toThrow('DOCUMENT_ORIGINAL_FRAME_REGION_INVALID')
    expect(observations.snapshot()?.regions).toEqual([])
  })
})
