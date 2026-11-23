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
    const boundary = vi.fn()
    const e = new DocumentPreviewPageEpoch(() => `PRIVATE_TOKEN_${++id}`, boundary)
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
    expect(reasons).toEqual(['initial', 'broad_spatial_change', 'page_changed_signal', 'source_changed', 'counter_reset', 'explicit_invalidation'])
    const broad = boundary.mock.calls[1][0]
    expect(broad).toMatchObject({ strong_cells: 17, quadrants: 3, exposure_offset_abs: 25 })
    expect(broad.mean_residual).toBeCloseTo(17 * 80 / 48)
    expect(JSON.stringify(boundary.mock.calls)).not.toMatch(/PRIVATE_|fingerprint|contentFingerprint|skinRatio|paperRatio/)
  })
  it('bounds the missing-frame duration and reports one boundary until paper returns', () => {
    const boundary = vi.fn(), e = new DocumentPreviewPageEpoch(() => 'page', boundary)
    e.observe('camera', 1, 0, frame())
    const dark = { ...frame(0), paperRatio: 0, brightness: 0 }
    e.observe('camera', 2, 500, dark)
    e.observe('camera', 3, 1_000_000, dark)
    e.observe('camera', 4, 1_500_000, dark)
    expect(boundary).toHaveBeenCalledTimes(2)
    expect(boundary.mock.calls[1][0]).toMatchObject({ preview_boundary_reason: 'blackout_or_page_missing', missing_elapsed_ms: 300_000 })
    for (const [details] of boundary.mock.calls) {
      for (const [key, max] of Object.entries({ strong_cells: 48, quadrants: 4, mean_residual: 255, exposure_offset_abs: 255, missing_elapsed_ms: 300_000 })) {
        if (details[key] !== undefined) expect(details[key]).toBeGreaterThanOrEqual(0)
        if (details[key] !== undefined) expect(details[key]).toBeLessThanOrEqual(max)
      }
    }
  })
  it('does not let a throwing diagnostic callback change page-boundary decisions', () => {
    let id = 0
    const e = new DocumentPreviewPageEpoch(() => `page-${++id}`, () => { throw new Error('diagnostics unavailable') })
    const first = e.observe('camera', 1, 0, frame())
    expect(e.observe('camera', 2, 500, spatialFrame(broadCells))).not.toBe(first)
    expect(e.observe('camera', 3, 1_000, spatialFrame(broadCells))).toBe('page-2')
  })
  it('retains a page through handwriting and partial hand coverage', () => {
    const e = setup(), first = e.observe('camera', 1, 0, frame())
    expect(e.observe('camera', 2, 500, { ...frame(142), skinRatio: .2 })).toBe(first)
    expect(e.observe('camera', 3, 1_000, frame(145))).toBe(first)
  })
  it.each([.401, .12, .08])('isolates distributed warm-page changes before formal capture at paper ratio %s', paperRatio => {
    const e = setup(), initial = { ...frame(), skinRatio: .118, paperRatio }
    const first = e.observe('camera', 1, 0, initial)
    expect(e.observe('camera', 2, 500, { ...spatialFrame(broadCells), skinRatio: .118, paperRatio })).not.toBe(first)
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
      const e = setup(), first = e.observe('camera', 1, 0, frame())
      expect(e.observe('camera', 2, 500, current)).toBe(first)
    }
  })
  it('does not treat fresh features from a repeated decoded frame as a page boundary', () => {
    const e = setup(), first = e.observe('camera', 1, 0, frame())
    expect(e.observe('camera', 1, 500, spatialFrame(broadCells))).toBe(first)
    expect(e.observe('camera', 2, 1_000, spatialFrame(broadCells))).not.toBe(first)
  })
  it('detects distributed structural changes under a simultaneous exposure shift', () => {
    const e = setup(), first = e.observe('camera', 1, 0, { ...frame(), skinRatio: .118 })
    const current = spatialFrame(broadCells, 60)
    current.fingerprint = current.fingerprint.map(value => value + 20)
    expect(e.observe('camera', 2, 500, { ...current, skinRatio: .118 })).not.toBe(first)
  })
  it('isolates sustained missing paper once, ignores repeated decoded frames', () => {
    const e = setup(), first = e.observe('camera', 1, 0, frame())
    const dark = { ...frame(0), paperRatio: 0, brightness: 0 }
    expect(e.observe('camera', 2, 500, dark)).toBe(first)
    expect(e.observe('camera', 2, 2_000, dark)).toBe(first)
    const next = e.observe('camera', 3, 2_100, dark)
    expect(next).not.toBe(first)
    expect(e.observe('camera', 4, 3_000, dark)).toBe(next)
    expect(e.observe('camera', 5, 3_500, frame())).toBe(next)
  })
  it('isolates explicit page change, new lease, counter reset and lifecycle reset', () => {
    const e = setup(), ids = [e.observe('camera', 1, 0, frame())]
    ids.push(e.observe('camera', 2, 500, frame(), true))
    ids.push(e.observe('new-lease', 3, 1_000, frame()))
    ids.push(e.observe('new-lease', 1, 1_500, frame()))
    e.reset(); ids.push(e.observe('new-lease', 2, 2_000, frame()))
    expect(new Set(ids).size).toBe(5)
  })
})
