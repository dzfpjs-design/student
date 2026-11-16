import { readFileSync } from 'node:fs'
import { gunzipSync } from 'node:zlib'
import { describe, expect, it } from 'vitest'
import { DocumentFrameMonitor, extractDocumentFrameFeatures } from './documentFrameMonitor'

const source = JSON.parse(gunzipSync(readFileSync('src/renderer/src/test/writingPaperFrame.json.gz')).toString())
const page = () => ({ ...source, data: new Uint8ClampedArray(source.data) })
function strokes(frame: ReturnType<typeof page>, shift = 0, color = 32): void {
  for (const left of [100, 108, 116]) for (let y = 350; y < 362; y++) for (let x = left + shift; x < left + shift + 2; x++) {
    frame.data.set([color, color, color, 255], (y * frame.width + x) * 4)
  }
}
const measure = extractDocumentFrameFeatures
const settle = (monitor: DocumentFrameMonitor, frame: ReturnType<typeof measure>, at: number) => {
  let result = monitor.observe(frame, at)
  for (let i = 1; i <= 4; i++) result = monitor.observe(frame, at + i * 400)
  return result
}

describe('local writing changes between OCR captures', () => {
  it.each(['negative sign', '2 to 5', 'decimal point'])('sees a small mathematical %s change inside a coarse cell', kind => {
    const first = page(), next = page()
    const pixel = (f: ReturnType<typeof page>, x: number, y: number) => f.data.set([32, 32, 32, 255], (y * f.width + x) * 4)
    if (kind === '2 to 5') {
      for (const [frame, rows] of [[first, ['111', '001', '111', '100', '111']], [next, ['111', '100', '111', '001', '111']]] as const) {
        rows.forEach((row, y) => [...row].forEach((value, x) => {
          if (value === '1') for (let dy = 0; dy < 2; dy++) pixel(frame, 100 + x, 352 + y * 2 + dy)
        }))
      }
    } else {
      strokes(first); strokes(next)
      for (let y = 354; y < 356; y++) for (let x = 88; x < (kind === 'negative sign' ? 96 : 90); x++) pixel(next, x, y)
    }
    const before = measure(first), after = measure(next), monitor = new DocumentFrameMonitor()
    settle(monitor, before, 0)
    monitor.markCaptured(before.fingerprint, 1600, before.contentFingerprint)
    expect(settle(monitor, after, 20000).captureRecommended).toBe(true)
  })

  it.each([[1, 0], [0, 1], [-1, -1], [2, 0], [0, -2], [2, -2]])('does not confuse a whole-frame %i/%i pixel jitter with writing', (dx, dy) => {
    const original = page(), shifted = page()
    for (let y = 0; y < original.height; y++) for (let x = 0; x < original.width; x++) {
      const sx = Math.max(0, Math.min(original.width - 1, x - dx)), sy = Math.max(0, Math.min(original.height - 1, y - dy))
      shifted.data.set(original.data.slice((sy * original.width + sx) * 4, (sy * original.width + sx) * 4 + 4), (y * original.width + x) * 4)
    }
    const before = measure(original), monitor = new DocumentFrameMonitor()
    settle(monitor, before, 0)
    monitor.markCaptured(before.fingerprint, 1600, before.contentFingerprint)
    expect(settle(monitor, measure(shifted), 20000).captureRecommended).toBe(false)
  })

  it.each(['half-pixel jitter', 'isolated bad pixels'])('does not repeatedly OCR %s without changed handwriting', kind => {
    const original = page(), changed = page()
    if (kind === 'half-pixel jitter') {
      for (let y = 0; y < original.height; y++) for (let x = 1; x < original.width; x++) {
        const offset = (y * original.width + x) * 4
        for (let c = 0; c < 3; c++) changed.data[offset + c] = (original.data[offset + c] + original.data[offset - 4 + c]) / 2
      }
    } else {
      for (const x of [80, 100, 120, 140]) changed.data.set([32, 32, 32, 255], (350 * changed.width + x) * 4)
    }
    const before = measure(original), monitor = new DocumentFrameMonitor()
    settle(monitor, before, 0)
    monitor.markCaptured(before.fingerprint, 1600, before.contentFingerprint)
    expect(settle(monitor, measure(changed), 20000).captureRecommended).toBe(false)
  })
  it('recognizes the first short writing on otherwise blank paper', () => {
    const frame = page()
    for (let i = 0; i < frame.data.length; i += 4) frame.data.set([255, 255, 255, 255], i)
    strokes(frame)
    const measured = measure(frame)
    expect(measured.contrast).toBeLessThan(7)
    expect(settle(new DocumentFrameMonitor(), measured, 0).captureRecommended).toBe(true)
  })
  it.each(['new writing', 'erasure', 'equal-ink rewrite'])('captures settled %s before the five-minute refresh', kind => {
    const first = page(), next = page()
    if (kind !== 'new writing') strokes(first)
    if (kind !== 'erasure') strokes(next, kind === 'equal-ink rewrite' ? 4 : 0)
    const before = measure(first), after = measure(next)
    const coarseDifference = before.fingerprint.reduce((sum, v, i) => sum + Math.abs(v - after.fingerprint[i]), 0) / 48
    expect(coarseDifference).toBeLessThan(3.5)
    const monitor = new DocumentFrameMonitor()
    expect(settle(monitor, before, 0).captureRecommended).toBe(true)
    monitor.markCaptured(before.fingerprint, 1600, before.contentFingerprint)
    expect(settle(monitor, after, 2000).captureRecommended).toBe(false)
    expect(settle(monitor, after, 18000).captureRecommended).toBe(true)
    monitor.markCaptured(after.fingerprint, 19600, after.contentFingerprint)
    expect(settle(monitor, after, 36000).captureRecommended).toBe(false)
  })

  it('waits for the new writing to settle even without a skin-colored hand in view', () => {
    const monitor = new DocumentFrameMonitor(), first = measure(page())
    settle(monitor, first, 0)
    monitor.markCaptured(first.fingerprint, 1600, first.contentFingerprint)
    for (let i = 0; i < 6; i++) {
      const next = page(); strokes(next, i * 4)
      const result = monitor.observe(measure(next), 18000 + i * 400)
      expect(result.state).toBe('writing')
      expect(result.captureRecommended).toBe(false)
    }
  })

  it.each(['dim', 'bright', 'gain', 'noise'])('does not spend another OCR capture on %s alone', kind => {
    const monitor = new DocumentFrameMonitor(), original = page(), changed = page()
    for (let i = 0; i < changed.data.length; i += 4) for (let c = 0; c < 3; c++) {
      changed.data[i + c] = kind === 'gain' ? changed.data[i + c] * .985
        : changed.data[i + c] + (kind === 'dim' ? -3 : kind === 'bright' ? 3 : (i / 4) % 3 - 1)
    }
    const before = measure(original)
    settle(monitor, before, 0)
    monitor.markCaptured(before.fingerprint, 1600, before.contentFingerprint)
    expect(settle(monitor, measure(changed), 18000).captureRecommended).toBe(false)
  })

  it('keeps the captured content snapshot when another frame arrives while OCR is pending', () => {
    const monitor = new DocumentFrameMonitor(), before = measure(page()), second = page()
    strokes(second); const after = measure(second)
    settle(monitor, before, 0)
    settle(monitor, after, 18000)
    monitor.markCaptured(before.fingerprint, 20000, before.contentFingerprint)
    expect(settle(monitor, after, 36000).captureRecommended).toBe(true)
  })
})
