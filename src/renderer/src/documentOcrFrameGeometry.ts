export type DocumentOcrFrameSource = Readonly<{
  /** SHA-256 of the exact JPEG bytes supplied to local OCR. */
  sha256: string
  width: number
  height: number
}>

export type DocumentOcrObservedLine = Readonly<{
  text: string
  confidence: number
  /** Normalized to the original camera frame, before any page registration. */
  box: Readonly<{ x: number; y: number; width: number; height: number }>
}>

type Point = readonly [number, number]
type Matrix = readonly (readonly [number, number, number])[]
const DIGEST = /^[0-9a-f]{64}$/
const MIN_LINE_CONFIDENCE = 0.90
const MAX_TEXT_LENGTH = 128 * 1024
// Match the backend's projection-rounding tolerance, then clip only that drift.
const POLYGON_TOLERANCE = 0.01

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function digest(value: unknown): value is string {
  return typeof value === 'string' && DIGEST.test(value)
}

function dimensions(width: number, height: number): boolean {
  return [width, height].every(value => Number.isSafeInteger(value) && value >= 80 && value <= 32_768)
}

function quad(value: unknown): Point[] | null {
  if (!Array.isArray(value) || value.length !== 4) return null
  const result: Point[] = []
  for (const point of value) {
    if (!Array.isArray(point) || point.length !== 2
      || !Number.isFinite(point[0]) || !Number.isFinite(point[1])) return null
    result.push([point[0], point[1]])
  }
  return result
}

function matchingQuads(left: Point[], right: Point[]): boolean {
  return left.every(([x, y], index) => x === right[index][0] && y === right[index][1])
}

function transformMatrix(value: unknown): Matrix | null {
  if (!Array.isArray(value) || value.length !== 3) return null
  const result: [number, number, number][] = []
  for (const row of value) {
    if (!Array.isArray(row) || row.length !== 3
      || !Number.isFinite(row[0]) || !Number.isFinite(row[1]) || !Number.isFinite(row[2])) return null
    result.push([row[0], row[1], row[2]])
  }
  return result
}

function projectsToOcr(frame: Point[], ocr: Point[], matrix: Matrix): boolean {
  return frame.every(([x, y], index) => {
    const [a, b, c] = matrix.map(row => row[0] * x + row[1] * y + row[2])
    return [a, b, c].every(Number.isFinite) && Math.abs(c) >= 1e-9
      && Math.abs(a / c - ocr[index][0]) <= POLYGON_TOLERANCE
      && Math.abs(b / c - ocr[index][1]) <= POLYGON_TOLERANCE
  })
}

function normalizedFrameBox(
  polygon: Point[],
  width: number,
  height: number
): DocumentOcrObservedLine['box'] | null {
  if (polygon.some(([x, y]) => x < -POLYGON_TOLERANCE || x > width - 1 + POLYGON_TOLERANCE
    || y < -POLYGON_TOLERANCE || y > height - 1 + POLYGON_TOLERANCE)) return null

  const points = polygon.map(([x, y]): Point => [
    Math.min(width - 1, Math.max(0, x)),
    Math.min(height - 1, Math.max(0, y))
  ])
  const turns = points.map(([x, y], i) => {
    const [nextX, nextY] = points[(i + 1) % 4]
    const [endX, endY] = points[(i + 2) % 4]
    return (nextX - x) * (endY - nextY) - (nextY - y) * (endX - nextX)
  })
  if (!turns.every(turn => turn > 0) && !turns.every(turn => turn < 0)) return null
  const twiceArea = points.reduce((sum, [x, y], i) => {
    const [nextX, nextY] = points[(i + 1) % 4]
    return sum + x * nextY - y * nextX
  }, 0)
  const minX = Math.min(...points.map(([x]) => x))
  const minY = Math.min(...points.map(([, y]) => y))
  const boxWidth = Math.max(...points.map(([x]) => x)) - minX
  const boxHeight = Math.max(...points.map(([, y]) => y)) - minY
  if (Math.abs(twiceArea) < 2 || boxWidth < 2 || boxHeight < 2) return null
  return { x: minX / width, y: minY / height, width: boxWidth / width, height: boxHeight / height }
}

async function textDigest(text: string): Promise<string> {
  const bytes = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return Array.from(new Uint8Array(bytes), value => value.toString(16).padStart(2, '0')).join('')
}

/**
 * Adapt the authenticated local monitor OCR response for one exact original JPEG.
 * Empty output means no admissible positive reading. It provides no evidence that
 * any region is blank, visible, on the same page, or obscured specifically by a hand.
 */
export async function readDocumentOcrFrameGeometry(
  artifact: unknown,
  source: DocumentOcrFrameSource
): Promise<readonly DocumentOcrObservedLine[]> {
  const { sha256, width, height } = source
  if (!digest(sha256) || !dimensions(width, height)) return []

  const response = record(artifact)
  const details = record(response?.details)
  const pipeline = record(details?.local_pipeline)
  const quality = record(pipeline?.quality)
  const geometry = record(pipeline?.text_geometry)
  if (response?.artifact_type !== 'document_ocr'
    || (response.status !== 'completed' && response.status !== 'quality_warning')
    || details?.monitor_capture_kind !== 'single_frame' || details.monitor_frame_sha256 !== sha256
    || details.engine !== 'rapidocr-onnx' || pipeline?.engine !== details.engine
    || pipeline.image_sha256 !== sha256 || record(pipeline.privacy)?.raw_image_uploaded !== false
    || record(quality?.orientation_evidence)?.ambiguous !== false
    || geometry?.version !== 'ocr_text_geometry_v1' || geometry.frame_sha256 !== sha256
    || !Array.isArray(geometry.frame_size) || geometry.frame_size.length !== 2
    || geometry.frame_size[0] !== width || geometry.frame_size[1] !== height
    || !digest(geometry.geometry_sha256) || !digest(geometry.text_sha256)
    || !Array.isArray(details.lines) || !Array.isArray(geometry.lines)
    || details.lines.length === 0 || details.lines.length > 4096
    || details.lines.length !== geometry.lines.length) return []

  const frameToOcr = transformMatrix(geometry.frame_to_ocr)
  if (!frameToOcr) return []

  const observed: DocumentOcrObservedLine[] = []
  const sourceText: { text: string; sha256: string }[] = []
  let offset = 0
  let textLength = 0
  for (let i = 0; i < details.lines.length; i++) {
    const line = record(details.lines[i])
    const span = record(geometry.lines[i])
    const text = line?.text
    if (typeof text !== 'string' || !text.trim() || /[\r\n\uD800-\uDFFF]/u.test(text)) return []
    textLength += text.length + 1
    if (textLength > MAX_TEXT_LENGTH) return []
    // Backend span positions are Unicode code-point offsets, not UTF-16 indices.
    const end = offset + Array.from(text).length
    if (span?.line_index !== i || span.start !== offset || span.end !== end || !digest(span.text_sha256)) return []
    offset = end + 1
    sourceText.push({ text, sha256: span.text_sha256 })

    const confidence = line?.confidence
    if (typeof confidence !== 'number' || !Number.isFinite(confidence)
      || confidence < MIN_LINE_CONFIDENCE || confidence > 1) continue
    const ocrQuad = quad(line?.box)
    const boundOcrQuad = quad(span.ocr_polygon)
    const frameQuad = quad(span.frame_polygon)
    if (!ocrQuad || !boundOcrQuad || !frameQuad || !matchingQuads(ocrQuad, boundOcrQuad)
      || !projectsToOcr(frameQuad, boundOcrQuad, frameToOcr)) continue
    const box = normalizedFrameBox(frameQuad, width, height)
    if (box) observed.push({ text, confidence, box })
  }

  if (observed.length === 0) return []
  const extractedText = sourceText.map(({ text }) => text).join('\n')
  const expectedTextDigest = geometry.text_sha256
  if (response.extracted_text !== extractedText) return []
  try {
    const checks = await Promise.all([
      textDigest(extractedText).then(actual => actual === expectedTextDigest),
      ...sourceText.map(async ({ text, sha256: expected }) => await textDigest(text) === expected)
    ])
    return checks.every(Boolean) ? observed : []
  } catch {
    // Preserve the original user workflow if Web Crypto is unavailable.
    return []
  }
}
