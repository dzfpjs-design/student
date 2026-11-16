import { readFileSync } from 'node:fs'
import { gunzipSync } from 'node:zlib'
import { expect, it } from 'vitest'
import { DocumentFrameMonitor, extractDocumentFrameFeatures } from './documentFrameMonitor'

const source = JSON.parse(gunzipSync(readFileSync('src/renderer/src/test/writingPaperFrame.json.gz')).toString())
const pixels = () => ({ ...source, data: new Uint8ClampedArray(source.data) })
const features = () => extractDocumentFrameFeatures(pixels())
it('recognizes the actual Electron text-on-paper frame before requesting OCR', () => {
  const measured = features()
  const monitor = new DocumentFrameMonitor()
  let decision = monitor.observe(measured, 0)
  for (let i = 1; i < 5; i++) decision = monitor.observe(measured, i * 1000)
  expect(decision.state).toBe('stable')
  expect(decision.captureRecommended).toBe(true)
})

it.each(['dark', 'blank', 'occluded'] as const)('keeps %s actual-frame variants out of OCR with bounded confidence', kind => {
  const frame = pixels()
  for (let i = 0; i < frame.data.length; i += 4) {
    const x = (i / 4) % frame.width
    if (kind === 'dark') for (let k = 0; k < 3; k++) frame.data[i + k] *= .05
    if (kind === 'blank') frame.data.set([255,255,255,255], i)
    if (kind === 'occluded' && x < frame.width * .4) frame.data.set([205,130,90,255], i)
  }
  const monitor = new DocumentFrameMonitor({ stableSamples: 1 })
  const decision = monitor.observe(extractDocumentFrameFeatures(frame), 0)
  expect(decision.state).toBe(kind === 'occluded' ? 'occluded' : 'no_page')
  expect(decision.captureRecommended).toBe(false)
  expect(decision.confidence).toBeGreaterThanOrEqual(0)
  expect(decision.confidence).toBeLessThanOrEqual(1)
})

it('retains area fingerprints and cooldown for a moved page, not immediate repeated OCR', () => {
  const original = pixels(), moved = pixels()
  for (let y = 0; y < moved.height; y++) for (let x = 0; x < moved.width; x++) {
    const i = (y * moved.width + x) * 4
    moved.data.set(x >= 80 ? original.data.slice((y * moved.width + x - 80) * 4, (y * moved.width + x - 80) * 4 + 4) : [51,51,51,255], i)
  }
  const first = features(), second = extractDocumentFrameFeatures(moved)
  const monitor = new DocumentFrameMonitor({ stableSamples: 2 })
  monitor.observe(first, 0)
  expect(monitor.observe(first, 1000).captureRecommended).toBe(true)
  monitor.markCaptured(first.fingerprint, 1000)
  expect(monitor.observe(first, 17000).captureRecommended).toBe(false)
  const moving = monitor.observe(second, 18000)
  expect(moving.state).toBe('page_changed')
  expect(moving.captureRecommended).toBe(false)
  expect(monitor.observe(second, 19000).captureRecommended).toBe(true)
})

it('does not treat a small uniform exposure fluctuation in the real frame as new writing', () => {
  const first = features(), dimmer = pixels()
  for (let i = 0; i < dimmer.data.length; i += 4) for (let k = 0; k < 3; k++) dimmer.data[i + k] -= 3
  const monitor = new DocumentFrameMonitor({ stableSamples: 1 })
  expect(monitor.observe(first, 0).captureRecommended).toBe(true)
  monitor.markCaptured(first.fingerprint, 0)
  const observed = monitor.observe(extractDocumentFrameFeatures(dimmer), 16000)
  expect(observed.state).toBe('stable')
  expect(observed.captureRecommended).toBe(false)
})
