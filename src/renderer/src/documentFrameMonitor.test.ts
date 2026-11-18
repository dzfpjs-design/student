import { describe, expect, it } from 'vitest'
import { DocumentFrameMonitor, documentFrameDiagnosticBands, extractDocumentFrameFeatures, type DocumentFrameFeatures } from './documentFrameMonitor'

function frame(overrides: Partial<DocumentFrameFeatures> = {}): DocumentFrameFeatures {
  return {
    brightness: 180,
    contrast: 42,
    paperRatio: 0.72,
    skinRatio: 0.01,
    fingerprint: new Uint8Array(48).fill(140),
    ...overrides
  }
}

function syntheticPage(shift: number, phase: number, extraInk = false, edgeInk?: 'left' | 'right' | 'top' | 'bottom') {
  const width = 320, height = 240, data = new Uint8ClampedArray(width * height * 4)
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
    const pageX = x - shift
    const printed = pageX >= 32 && pageX < 288 && y >= 24 && y < 200 && y % 20 < 2 && Math.floor(pageX / 6) % 3 !== 0
    const correction = extraInk && pageX >= 90 && pageX < 100 && y >= 210 && y < 214
    const edgeCorrection = (edgeInk === 'left' && x === 0 && y >= 80 && y < 120)
      || (edgeInk === 'right' && x === width - 1 && y >= 80 && y < 120)
      || (edgeInk === 'top' && y === 0 && x >= 80 && x < 120)
      || (edgeInk === 'bottom' && y === height - 1 && x >= 80 && x < 120)
    const noise = (((x * 19 + y * 31 + phase * 13) % 31) - 15) * .45
    const value = Math.max(0, Math.min(255, (printed || correction || edgeCorrection ? 45 : 190) + noise))
    const offset = (y * width + x) * 4
    data[offset] = data[offset + 1] = data[offset + 2] = value
    data[offset + 3] = 255
  }
  return extractDocumentFrameFeatures({ width, height, data })
}

describe('continuous document frame monitor', () => {
  it('accepts only a fresh clear draft matching the last stable sample', () => {
    const monitor = new DocumentFrameMonitor({ stableSamples: 2 })
    const clear = syntheticPage(0, 0)
    monitor.observeFreshFrame(clear, 0, 10)
    monitor.observeFreshFrame(clear, 500, 11)
    expect(monitor.matchesStableFrame(clear, 600, 12)).toBe(true)
    expect(monitor.matchesStableFrame({ ...clear, skinRatio: .3 }, 600, 12)).toBe(false)
    expect(monitor.matchesStableFrame(syntheticPage(0, 0, true), 600, 12)).toBe(false)
    expect(monitor.matchesStableFrame(clear, 2001, 12)).toBe(false)
    monitor.reset()
    expect(monitor.matchesStableFrame(clear, 600, 12)).toBe(false)
  })

  it('does not accumulate stability from a frozen frame and requires a newer draft frame', () => {
    const monitor = new DocumentFrameMonitor({ stableSamples: 4 })
    const clear = syntheticPage(0, 0)
    expect(monitor.observeFreshFrame(clear, 0, 10)?.state).toBe('stabilizing')
    for (const at of [500, 1000, 1500, 2000]) expect(monitor.observeFreshFrame(clear, at, 10)).toBeNull()
    expect(monitor.matchesStableFrame(clear, 2100, 10)).toBe(false)
    for (const [token, at] of [[11, 2500], [12, 3000]] as const) {
      expect(monitor.observeFreshFrame(clear, at, token)?.state).toBe('stabilizing')
    }
    expect(monitor.observeFreshFrame(clear, 3500, 13)?.state).toBe('stable')
    expect(monitor.matchesStableFrame(clear, 3600, 13)).toBe(false)
    expect(monitor.matchesStableFrame(clear, 3600, 14)).toBe(true)
    // A reset device counter must rebuild stability, then can recover.
    expect(monitor.observeFreshFrame(clear, 4000, 1)?.state).toBe('stabilizing')
    expect(monitor.matchesStableFrame(clear, 4100, 2)).toBe(false)
    for (const [token, at] of [[2, 4500], [3, 5000]] as const) {
      expect(monitor.observeFreshFrame(clear, at, token)?.state).toBe('stabilizing')
    }
    expect(monitor.observeFreshFrame(clear, 5500, 4)?.state).toBe('stable')
    expect(monitor.matchesStableFrame(clear, 5600, 5)).toBe(true)
  })
  it('exports only coarse, bounded diagnostic bands without frame content', () => {
    const monitor = new DocumentFrameMonitor()
    const sample = syntheticPage(0, 0)
    const first = monitor.observe(sample, 0)
    const shifted = syntheticPage(2, 1, true)
    const next = monitor.observe(shifted, 500)
    expect(documentFrameDiagnosticBands(first, sample)).toEqual({
      frame_motion_band: 'lt_2', frame_skin_band: 'lt_025', frame_content_motion: 'unavailable',
      frame_paper_band: 'ge_10', frame_light_band: 'usable',
    })
    expect(documentFrameDiagnosticBands(next, shifted).frame_content_motion).toBe('changed')
    expect(JSON.stringify(documentFrameDiagnosticBands(next, shifted))).not.toContain('fingerprint')
  })
  it('requires a stable, unoccluded page before recommending OCR', () => {
    const monitor = new DocumentFrameMonitor({ stableSamples: 3, minimumCaptureIntervalMs: 10_000 })
    expect(monitor.observe(frame(), 0).state).toBe('stabilizing')
    expect(monitor.observe(frame(), 500).captureRecommended).toBe(false)
    const stable = monitor.observe(frame(), 1_000)
    expect(stable.state).toBe('stable')
    expect(stable.captureRecommended).toBe(true)
  })

  it('treats small USB auto-exposure fluctuations as a stable page', () => {
    const monitor = new DocumentFrameMonitor({ stableSamples: 3 })
    const fingerprints = [140, 143, 139].map((value) => new Uint8Array(48).fill(value))

    expect(monitor.observe(frame({ fingerprint: fingerprints[0] }), 0).state).toBe('stabilizing')
    expect(monitor.observe(frame({ fingerprint: fingerprints[1] }), 450).state).toBe('stabilizing')
    const settled = monitor.observe(frame({ fingerprint: fingerprints[2] }), 900)

    expect(settled.state).toBe('stable')
    expect(settled.captureRecommended).toBe(true)
  })

  it('settles a stationary page despite small image jitter and compression noise', () => {
    const monitor = new DocumentFrameMonitor()
    let decision = monitor.observe(syntheticPage(0, 0), 0)
    for (let i = 1; i < 8; i++) decision = monitor.observe(syntheticPage(i % 2 ? 2 : 0, i % 2), i * 500)
    expect(decision.state).toBe('stable')
    expect(decision.captureRecommended).toBe(true)
  })

  it('still detects a small new written mark after accounting for image jitter', () => {
    const monitor = new DocumentFrameMonitor({ stableSamples: 2, minimumCaptureIntervalMs: 1000 })
    const first = syntheticPage(0, 0)
    monitor.observe(first, 0)
    expect(monitor.observe(syntheticPage(2, 1), 500).captureRecommended).toBe(true)
    const captured = syntheticPage(2, 1)
    monitor.markCaptured(captured.fingerprint, 500, captured.contentFingerprint)
    expect(monitor.observe(syntheticPage(0, 0, true), 1500).state).toBe('writing')
    const updated = monitor.observe(syntheticPage(2, 1, true), 2000)
    expect(updated.state).toBe('stabilizing')
    expect(monitor.observe(syntheticPage(0, 0, true), 2500).captureRecommended).toBe(true)
  })

  it.each(['left', 'right', 'top', 'bottom'] as const)(
    'detects new ink at the %s edge and schedules another capture', (edge) => {
      const monitor = new DocumentFrameMonitor({ stableSamples: 1, minimumCaptureIntervalMs: 1000 })
      const before = syntheticPage(0, 0)
      monitor.observe(before, 0)
      monitor.markCaptured(before.fingerprint, 0, before.contentFingerprint)
      const after = syntheticPage(0, 0, false, edge)
      expect(monitor.observe(after, 1500).state).toBe('writing')
      expect(monitor.matchesCapturedPage(after)).toBe(false)
      const settled = monitor.observe(after, 2000)
      expect(settled.state).toBe('stable')
      expect(settled.captureRecommended).toBe(true)
    }
  )

  it('does not treat a lone moved mark on an otherwise blank page as page jitter', () => {
    function sparse(markOffset: number) {
      const width = 320, height = 240, data = new Uint8ClampedArray(width * height * 4)
      for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
        const ink = x >= 120 + markOffset && x < 128 + markOffset && y >= 120 && y < 126
        const offset = (y * width + x) * 4
        data[offset] = data[offset + 1] = data[offset + 2] = ink ? 40 : 190
        data[offset + 3] = 255
      }
      return extractDocumentFrameFeatures({ width, height, data })
    }
    const monitor = new DocumentFrameMonitor({ stableSamples: 1, minimumCaptureIntervalMs: 1000 })
    const before = sparse(0)
    monitor.observe(before, 0)
    monitor.markCaptured(before.fingerprint, 0, before.contentFingerprint)
    const changed = sparse(2)
    expect(monitor.observe(changed, 2000).state).toBe('writing')
    expect(monitor.matchesCapturedPage(changed)).toBe(false)
  })

  it('classifies hand motion as writing and does not capture through occlusion', () => {
    const monitor = new DocumentFrameMonitor({ stableSamples: 2 })
    monitor.observe(frame(), 0)
    const moving = frame({
      skinRatio: 0.08,
      fingerprint: new Uint8Array(48).map((_, index) => index % 2 ? 210 : 70)
    })
    expect(monitor.observe(moving, 500).state).toBe('writing')
    const covered = frame({ skinRatio: 0.28 })
    const result = monitor.observe(covered, 1_000)
    expect(result.state).toBe('occluded')
    expect(result.captureRecommended).toBe(false)
  })

  it('emits a page change and captures only after the new page settles', () => {
    const monitor = new DocumentFrameMonitor({ stableSamples: 2, minimumCaptureIntervalMs: 10_000 })
    monitor.observe(frame(), 0)
    const first = monitor.observe(frame(), 500)
    expect(first.captureRecommended).toBe(true)
    monitor.markCaptured(frame().fingerprint, 500)

    const changed = frame({ fingerprint: new Uint8Array(48).fill(70) })
    expect(monitor.observe(changed, 1_000).state).toBe('page_changed')
    const settled = monitor.observe(changed, 1_500)
    expect(settled.state).toBe('stable')
    expect(settled.captureRecommended).toBe(false)
    expect(monitor.observe(changed, 10_499).captureRecommended).toBe(false)
    expect(monitor.observe(changed, 10_500).captureRecommended).toBe(true)
  })

  it('does not repeatedly OCR an unchanged page before the refresh interval', () => {
    const monitor = new DocumentFrameMonitor({ stableSamples: 2, minimumCaptureIntervalMs: 10_000, refreshCaptureIntervalMs: 60_000 })
    monitor.observe(frame(), 0)
    monitor.observe(frame(), 500)
    monitor.markCaptured(frame().fingerprint, 500)
    expect(monitor.observe(frame(), 11_000).captureRecommended).toBe(false)
    expect(monitor.observe(frame(), 61_000).captureRecommended).toBe(true)
  })

  it('keeps scenes without a usable page out of the OCR path', () => {
    const monitor = new DocumentFrameMonitor()
    const result = monitor.observe(frame({ paperRatio: 0.04, brightness: 60 }), 0)
    expect(result.state).toBe('no_page')
    expect(result.captureRecommended).toBe(false)
  })

  it('retries a failed OCR attempt after cooldown without treating it as saved content', () => {
    const monitor = new DocumentFrameMonitor({ stableSamples: 1, minimumCaptureIntervalMs: 10_000 })
    expect(monitor.observe(frame(), 0).captureRecommended).toBe(true)
    monitor.markCaptureAttempt(0)
    expect(monitor.observe(frame(), 9_999).captureRecommended).toBe(false)
    expect(monitor.observe(frame(), 10_000).captureRecommended).toBe(true)
    monitor.markCaptured(frame().fingerprint, 10_000)
    expect(monitor.observe(frame(), 20_000).captureRecommended).toBe(false)
  })

  it('retries an invalidated OCR promptly once per minute, but still requires fresh stable frames', () => {
    const monitor = new DocumentFrameMonitor({ stableSamples: 2 })
    const clear = frame()
    monitor.observeFreshFrame(clear, 0, 1)
    expect(monitor.observeFreshFrame(clear, 500, 2)?.captureRecommended).toBe(true)
    monitor.markCaptureAttempt(500)
    monitor.observeFreshFrame({ ...clear, skinRatio: .3 }, 1000, 3)
    monitor.markStaleCaptureAttempt(1000)
    expect(monitor.observeFreshFrame(clear, 5000, 4)?.captureRecommended).toBe(false)
    expect(monitor.observeFreshFrame(clear, 5499, 5)?.captureRecommended).toBe(false)
    expect(monitor.observeFreshFrame(clear, 5500, 6)?.captureRecommended).toBe(true)
    monitor.markCaptureAttempt(5500)
    monitor.observeFreshFrame({ ...clear, skinRatio: .3 }, 6000, 7)
    monitor.markStaleCaptureAttempt(6000)
    monitor.observeFreshFrame(clear, 10_500, 8)
    expect(monitor.observeFreshFrame(clear, 10_501, 9)?.captureRecommended).toBe(false)
    expect(monitor.observeFreshFrame(clear, 20_500, 10)?.captureRecommended).toBe(true)
  })

  it('counts the limit when an early request starts and retains it through camera reset', () => {
    const monitor = new DocumentFrameMonitor({ stableSamples: 1 })
    monitor.markCaptureAttempt(0)
    monitor.markStaleCaptureAttempt(1000)
    expect(monitor.canCaptureAt(5000)).toBe(true)
    monitor.markCaptureAttempt(5000) // actual early request starts here
    monitor.reset() // device release must not reset either request budget
    expect(monitor.canCaptureAt(10_000)).toBe(false)
    monitor.markCaptureAttempt(20_000)
    monitor.markStaleCaptureAttempt(21_000)
    expect(monitor.canCaptureAt(25_000)).toBe(false)
    expect(monitor.canCaptureAt(35_000)).toBe(true)
    monitor.markCaptureAttempt(61_000)
    monitor.markStaleCaptureAttempt(62_000)
    expect(monitor.canCaptureAt(66_000)).toBe(true)
  })

  it('clears saved page identity but keeps the request interval across camera lifecycle', () => {
    const monitor = new DocumentFrameMonitor({ stableSamples: 1 })
    monitor.markCaptured(frame().fingerprint, 50_000)
    monitor.reset()
    expect(monitor.observe(frame(), 50_001).captureRecommended).toBe(false)
    expect(monitor.observe(frame(), 65_000).captureRecommended).toBe(true)
  })
})
