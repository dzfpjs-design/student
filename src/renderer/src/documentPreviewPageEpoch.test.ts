import { describe, expect, it } from 'vitest'
import { DocumentPreviewPageEpoch } from './documentPreviewPageEpoch'
import type { DocumentFrameFeatures } from './documentFrameMonitor'

const frame = (value = 140): DocumentFrameFeatures => ({ brightness: 180, contrast: 42, paperRatio: .7, skinRatio: .01, fingerprint: new Uint8Array(48).fill(value) })
function setup() { let id = 0; return new DocumentPreviewPageEpoch(() => `page-${++id}`) }

describe('document preview page boundary', () => {
  it('retains a page through handwriting and partial hand coverage', () => {
    const e = setup(), first = e.observe('camera', 1, 0, frame())
    expect(e.observe('camera', 2, 500, { ...frame(142), skinRatio: .2 })).toBe(first)
    expect(e.observe('camera', 3, 1_000, frame(145))).toBe(first)
  })
  it('isolates broad page changes before any formal capture', () => {
    const e = setup(), first = e.observe('camera', 1, 0, frame())
    expect(e.observe('camera', 2, 500, frame(165))).not.toBe(first)
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
