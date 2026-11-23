import { describe, expect, it, vi } from 'vitest'
import { DocumentPreviewPageEpoch } from './documentPreviewPageEpoch'
import type { DocumentFrameFeatures } from './documentFrameMonitor'

const frame = (value = 140): DocumentFrameFeatures => ({ brightness: 180, contrast: 42, paperRatio: .7, skinRatio: .01, fingerprint: new Uint8Array(48).fill(value) })
function setup() { let id = 0; return new DocumentPreviewPageEpoch(() => `page-${++id}`) }
const broadCells = [0, 1, 2, 8, 9, 10, 16, 17, 18, 4, 5, 12, 13, 24, 25, 32, 33]
function spatialFrame(cells: number[], change = 80): DocumentFrameFeatures {
  const current = frame()
  for (const cell of cells) current.fingerprint[cell] += change
  return current
}

describe('document preview page boundary', () => {
  it('reports each actual token creation once with closed reasons and no source identity', () => {
    let id = 0
    const boundary = vi.fn(), scene = vi.fn()
    const e = new DocumentPreviewPageEpoch(() => `PRIVATE_TOKEN_${++id}`, boundary, scene)
    e.observe('PRIVATE_SOURCE', 1, 0, frame())
    e.observe('PRIVATE_SOURCE', 1, 500, spatialFrame(broadCells), true)
    e.observe('PRIVATE_SOURCE', 2, 1_000, frame(165))
    expect(boundary.mock.calls.map(([details]) => details.preview_boundary_reason)).toEqual(['initial'])
    e.observe('PRIVATE_SOURCE', 3, 1_500, { ...spatialFrame(broadCells), skinRatio: .118 })
    e.observe('PRIVATE_SOURCE', 4, 2_000, { ...spatialFrame(broadCells), skinRatio: .118 })
    e.observe('PRIVATE_SOURCE', 5, 2_500, frame(), true)
    e.observe('PRIVATE_OTHER_SOURCE', 6, 3_000, frame())
    e.observe('PRIVATE_OTHER_SOURCE', 1, 3_500, frame())
    e.reset(); e.reset()
    e.observe('PRIVATE_OTHER_SOURCE', 2, 4_000, frame())
    const reasons = boundary.mock.calls.map(([details]) => details.preview_boundary_reason)
    expect(reasons).toEqual(['initial', 'source_changed', 'counter_reset', 'explicit_invalidation'])
    const broad = scene.mock.calls[0][0]
    expect(broad.preview_scene_reason).toBe('broad_spatial_change')
    expect(broad).toMatchObject({ strong_cells: 17, quadrants: 3, exposure_offset_abs: 25 })
    expect(broad.mean_residual).toBeCloseTo(17 * 80 / 48)
    expect(JSON.stringify([boundary.mock.calls, scene.mock.calls])).not.toMatch(/PRIVATE_|fingerprint|contentFingerprint|skinRatio|paperRatio/)
  })
  it('bounds the missing-frame duration and reports one boundary until paper returns', () => {
    const boundary = vi.fn(), scene = vi.fn(), e = new DocumentPreviewPageEpoch(() => 'page', boundary, scene)
    e.observe('camera', 1, 0, frame())
    const dark = { ...frame(0), paperRatio: 0, brightness: 0 }
    e.observe('camera', 2, 500, dark)
    e.observe('camera', 3, 1_000_000, dark)
    e.observe('camera', 4, 1_500_000, dark)
    expect(boundary).toHaveBeenCalledTimes(1)
    expect(scene).toHaveBeenCalledTimes(1)
    expect(scene.mock.calls[0][0]).toMatchObject({ preview_scene_reason: 'blackout_or_page_missing', missing_elapsed_ms: 300_000 })
    for (const [details] of [...boundary.mock.calls, ...scene.mock.calls]) {
      for (const [key, max] of Object.entries({ strong_cells: 48, quadrants: 4, mean_residual: 255, exposure_offset_abs: 255, missing_elapsed_ms: 300_000 })) {
        if (details[key] !== undefined) expect(details[key]).toBeGreaterThanOrEqual(0)
        if (details[key] !== undefined) expect(details[key]).toBeLessThanOrEqual(max)
      }
    }
  })
  it('does not let a throwing diagnostic callback change page-boundary decisions', () => {
    let id = 0
    const unavailable = () => { throw new Error('diagnostics unavailable') }
    const e = new DocumentPreviewPageEpoch(() => `page-${++id}`, unavailable, unavailable)
    const first = e.observe('camera', 1, 0, frame())
    expect(e.observe('camera', 2, 500, spatialFrame(broadCells))).toBe(first)
    expect(e.observe('another-camera', 3, 1_000, spatialFrame(broadCells))).toBe('page-2')
  })
  it('retains a page through handwriting and partial hand coverage', () => {
    const e = setup(), first = e.observe('camera', 1, 0, frame())
    expect(e.observe('camera', 2, 500, { ...frame(142), skinRatio: .2 })).toBe(first)
    expect(e.observe('camera', 3, 1_000, frame(145))).toBe(first)
  })
  it.each([.401, .12, .08])('retains the candidate domain through broad warm-scene changes at paper ratio %s', paperRatio => {
    const e = setup(), initial = { ...frame(), skinRatio: .118, paperRatio }
    const first = e.observe('camera', 1, 0, initial)
    expect(e.observe('camera', 2, 500, { ...spatialFrame(broadCells), skinRatio: .118, paperRatio })).toBe(first)
  })
  it('retains a page through a global exposure shift and warm-color classification changes', () => {
    const e = setup(), first = e.observe('camera', 1, 0, frame())
    expect(e.observe('camera', 2, 500, { ...frame(175), skinRatio: .118, brightness: 215 })).toBe(first)
    expect(e.observe('camera', 3, 1_000, { ...frame(125), skinRatio: .35, brightness: 165 })).toBe(first)
  })
  it('retains a page through exposure changes even without a warm-color signal', () => {
    const e = setup(), first = e.observe('camera', 1, 0, frame())
    expect(e.observe('camera', 2, 500, frame(165))).toBe(first)
  })
  it.each([{ name: 'single mark', cells: [19] },
    { name: 'central hand', cells: [18, 19, 20, 26, 27, 28, 34, 35, 36] },
    { name: 'half-frame hand', cells: Array.from({ length: 24 }, (_, index) => index) }])(
    'retains a page through a $name covering limited quadrants', ({ cells }) => {
      const e = setup(), first = e.observe('camera', 1, 0, frame())
      expect(e.observe('camera', 2, 500, { ...spatialFrame(cells), skinRatio: .3 })).toBe(first)
    })
  it('requires enough changed cells and overall residual contrast', () => {
    for (const current of [spatialFrame(broadCells.slice(0, 16)), spatialFrame(broadCells, 30)]) {
      const scene = vi.fn(), e = new DocumentPreviewPageEpoch(() => 'page', undefined, scene), first = e.observe('camera', 1, 0, frame())
      expect(e.observe('camera', 2, 500, current)).toBe(first)
      expect(scene).not.toHaveBeenCalled()
    }
  })
  it('does not treat fresh features from a repeated decoded frame as a page boundary', () => {
    const e = setup(), first = e.observe('camera', 1, 0, frame())
    expect(e.observe('camera', 1, 500, spatialFrame(broadCells))).toBe(first)
    expect(e.observe('camera', 2, 1_000, spatialFrame(broadCells))).toBe(first)
  })
  it('detects distributed structural changes under a simultaneous exposure shift', () => {
    const e = setup(), first = e.observe('camera', 1, 0, { ...frame(), skinRatio: .118 })
    const current = spatialFrame(broadCells, 60)
    current.fingerprint = current.fingerprint.map(value => value + 20)
    expect(e.observe('camera', 2, 500, { ...current, skinRatio: .118 })).toBe(first)
  })
  it('keeps missing-paper guesses in the candidate domain and ignores repeated decoded frames', () => {
    const e = setup(), first = e.observe('camera', 1, 0, frame())
    const dark = { ...frame(0), paperRatio: 0, brightness: 0 }
    expect(e.observe('camera', 2, 500, dark)).toBe(first)
    expect(e.observe('camera', 2, 2_000, dark)).toBe(first)
    const next = e.observe('camera', 3, 2_100, dark)
    expect(next).toBe(first)
    expect(e.observe('camera', 4, 3_000, dark)).toBe(next)
    expect(e.observe('camera', 5, 3_500, frame())).toBe(next)
  })
  it('isolates new lease, counter reset and explicit lifecycle reset without treating a page-change guess as identity', () => {
    const e = setup(), ids = [e.observe('camera', 1, 0, frame())]
    ids.push(e.observe('camera', 2, 500, frame(), true))
    ids.push(e.observe('new-lease', 3, 1_000, frame()))
    ids.push(e.observe('new-lease', 1, 1_500, frame()))
    e.reset(); ids.push(e.observe('new-lease', 2, 2_000, frame()))
    expect(ids[1]).toBe(ids[0])
    expect(new Set(ids).size).toBe(4)
  })

  it('never confirms a long stable hand pose by timeout or low motion', () => {
    let id = 0
    const scene = vi.fn(), e = new DocumentPreviewPageEpoch(() => `page-${++id}`, undefined, scene)
    const first = e.observe('camera', 1, 0, frame())
    for (let index = 2; index < 40; index++) {
      const current = spatialFrame(broadCells, index % 2 ? 80 : -60)
      expect(e.observe('camera', index, index * 60_000, { ...current, skinRatio: .118 }, index === 2)).toBe(first)
    }
    expect(scene.mock.calls.map(([details]) => details.preview_scene_reason)).toEqual(['page_changed_signal'])
  })

  it('requires fresh continuous content observations before cancelling a scene on reference return', () => {
    const scene = vi.fn(), e = new DocumentPreviewPageEpoch(() => 'page', undefined, scene)
    const reference = { ...frame(), contentFingerprint: { width: 8, height: 8,
      pixels: Uint8Array.from({ length: 64 }, (_, index) => index % 3 ? 210 : 70) } }
    e.observe('camera', 1, 0, reference)
    e.observe('camera', 2, 500, spatialFrame(broadCells))
    e.observe('camera', 2, 20_000, reference)
    expect(scene).toHaveBeenCalledTimes(1)
    for (let index = 3; index <= 5; index++) e.observe('camera', index, index * 500, reference)
    expect(scene).toHaveBeenCalledTimes(1)
    e.observe('camera', 6, 3_000, reference)
    expect(scene.mock.calls.map(([details]) => details.preview_scene_reason)).toEqual(['broad_spatial_change', 'reference_returned'])
  })

  it('does not accept coarse reference return without unchanged content evidence', () => {
    const scene = vi.fn(), e = new DocumentPreviewPageEpoch(() => 'page', undefined, scene)
    const reference = { ...frame(), contentFingerprint: { width: 8, height: 8, pixels: new Uint8Array(64).fill(70) } }
    e.observe('camera', 1, 0, reference)
    e.observe('camera', 2, 500, spatialFrame(broadCells))
    for (let index = 3; index <= 10; index++) {
      const wrongContent = { ...reference, contentFingerprint: { width: 8, height: 8, pixels: new Uint8Array(64).fill(210) } }
      e.observe('camera', index, index * 500, index < 7 ? frame() : wrongContent)
    }
    expect(scene).toHaveBeenCalledTimes(1)
  })

  it('keeps the pre-missing reference and never promotes elapsed paper absence to a new domain', () => {
    let id = 0
    const scene = vi.fn(), e = new DocumentPreviewPageEpoch(() => `page-${++id}`, undefined, scene)
    const reference = { ...frame(), contentFingerprint: { width: 8, height: 8,
      pixels: Uint8Array.from({ length: 64 }, (_, index) => index % 3 ? 210 : 70) } }
    const first = e.observe('camera', 1, 0, reference)
    const missing = { ...frame(0), brightness: 0, contrast: 0, paperRatio: 0 }
    expect(e.observe('camera', 2, 500, missing)).toBe(first)
    expect(e.observe('camera', 3, 1_500, missing)).toBe(first)
    expect(scene.mock.calls[0][0].preview_scene_reason).toBe('blackout_or_page_missing')
    for (let index = 4; index <= 7; index++) expect(e.observe('camera', index, index * 500, reference)).toBe(first)
    expect(scene.mock.calls.map(([details]) => details.preview_scene_reason)).toEqual(['blackout_or_page_missing', 'reference_returned'])
  })

  it.each(['source', 'counter', 'privacy-reset'])('discards pending references across %s without reviving the old domain', boundary => {
    let id = 0
    const scene = vi.fn(), e = new DocumentPreviewPageEpoch(() => `page-${++id}`, undefined, scene)
    const first = e.observe('camera', 10, 0, frame())
    e.observe('camera', 11, 500, spatialFrame(broadCells))
    if (boundary === 'privacy-reset') e.reset()
    const nextSource = boundary === 'source' ? 'new-lease' : 'camera'
    const nextFrame = boundary === 'counter' ? 1 : 12
    const next = e.observe(nextSource, nextFrame, 1_000, frame())
    expect(next).not.toBe(first)
    e.acknowledgeRegistered(first, 11, 1_500)
    expect(scene).toHaveBeenCalledTimes(1)
    expect(e.observe(nextSource, nextFrame + 1, 1_500, spatialFrame(broadCells))).toBe(next)
    e.acknowledgeRegistered(first, nextFrame + 1, 2_000)
    expect(scene).toHaveBeenCalledTimes(2)
  })

  it('accepts only a same-domain registered capture at or after the latest scene disturbance', () => {
    let id = 0
    const scene = vi.fn(), e = new DocumentPreviewPageEpoch(() => `page-${++id}`, undefined, scene)
    const first = e.observe('camera', 1, 0, frame())
    e.observe('camera', 2, 500, spatialFrame(broadCells))
    e.observe('camera', 3, 1_000, spatialFrame(broadCells, -60))
    e.acknowledgeRegistered(first, 2, 1_500)
    e.acknowledgeRegistered('another-domain', 3, 1_500)
    e.acknowledgeRegistered(first, 4, 1_500)
    expect(scene).toHaveBeenCalledTimes(1)
    e.acknowledgeRegistered(first, 3, 1_500)
    expect(scene.mock.calls[1][0].preview_scene_reason).toBe('registered')
    e.reset()
    const next = e.observe('camera', 4, 2_000, frame())
    e.observe('camera', 5, 2_500, spatialFrame(broadCells))
    e.acknowledgeRegistered(first, 5, 3_000)
    expect(scene).toHaveBeenCalledTimes(3)
    expect(next).not.toBe(first)
    e.acknowledgeRegistered(next, 5, 3_000)
    expect(scene).toHaveBeenCalledTimes(4)
  })
})
