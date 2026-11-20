// @vitest-environment node
import { createHash } from 'node:crypto'
import { describe, expect, it, vi } from 'vitest'
import { readDocumentOcrFrameGeometry } from './documentOcrFrameGeometry'

const sha = (text: string): string => createHash('sha256').update(text).digest('hex')
const source = { sha256: sha('synthetic original JPEG bytes'), width: 1200, height: 1600 }
const quad = [[120, 320], [360, 320], [360, 360], [120, 360]]

function artifactFixture(lines = [
  { text: '计划𠀀一', confidence: .96, frame: quad },
  { text: '第二行', confidence: .91, frame: [[120, 400], [400, 400], [400, 440], [120, 440]] }
]) {
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
    extracted_text: extractedText,
    details: {
      engine: 'rapidocr-onnx',
      monitor_capture_kind: 'single_frame',
      monitor_frame_sha256: source.sha256,
      lines: lines.map((line, i) => ({
        text: line.text,
        confidence: line.confidence,
        box: records[i].ocr_polygon.map(point => [...point])
      })),
      local_pipeline: {
        engine: 'rapidocr-onnx',
        image_sha256: source.sha256,
        privacy: { raw_image_uploaded: false },
        quality: { orientation_evidence: { ambiguous: false } },
        text_geometry: {
          version: 'ocr_text_geometry_v1',
          frame_sha256: source.sha256,
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
