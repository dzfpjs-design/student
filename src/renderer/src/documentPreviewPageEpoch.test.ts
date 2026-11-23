import { describe, expect, it } from 'vitest'
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
