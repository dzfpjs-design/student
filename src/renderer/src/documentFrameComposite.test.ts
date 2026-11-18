import { describe, expect, it } from 'vitest'
import { DocumentFrameComposite } from './documentFrameComposite'

const scope = 'session:child:lease:epoch'

function page(): ImageData {
  const width = 64, height = 64, data = new Uint8ClampedArray(width * height * 4)
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
    const i = (y * width + x) * 4
    const ink = x % 13 === 4 || (y % 13 === 5 && x % 6 < 4)
    data.set(ink ? [35, 35, 35, 255] : [240, 240, 240, 255], i)
  }
  return { data, width, height, colorSpace: 'srgb' } as ImageData
}

function cover(image: ImageData, left: number, right: number): ImageData {
  const copy = { ...image, data: new Uint8ClampedArray(image.data) } as ImageData
  for (let y = 18; y < 40; y++) for (let x = left; x < right; x++) copy.data.set([200, 120, 80, 255], (y * image.width + x) * 4)
  return copy
}

describe('document multiframe candidate', () => {
  it('keeps the current frame unchanged when the occlusion mask is not independently verified', () => {
    const original = page(), composite = new DocumentFrameComposite()
    composite.add(cover(original, 42, 52), scope, 1000)
    const latest = cover(original, 8, 18), before = latest.data.slice()
    const result = composite.build(latest, scope, 1800)
    expect(result).toEqual({ kind: 'fallback', reason: 'unverified_occlusion_mask' })
    expect(latest.data).toEqual(before)
  })

  it('does not reuse another child/session or an expired frame', () => {
    const original = page(), composite = new DocumentFrameComposite()
    composite.add(cover(original, 42, 52), scope, 1000)
    expect(composite.build(cover(original, 8, 18), 'other-child', 1800).kind).toBe('fallback')
    expect(composite.build(cover(original, 8, 18), scope, 9000).kind).toBe('fallback')
  })

  it('rejects different pages, insufficient visual landmarks, changed dimensions and clock reversal', () => {
    const original = page(), composite = new DocumentFrameComposite()
    composite.add(cover(original, 42, 52), scope, 1000)
    const changed = page()
    for (let y = 0; y < 64; y++) for (let x = 0; x < 64; x++) {
      const i = (y * 64 + x) * 4
      changed.data[i] = 255 - changed.data[i]; changed.data[i + 1] = 255 - changed.data[i + 1]; changed.data[i + 2] = 255 - changed.data[i + 2]
    }
    expect(composite.build(cover(changed, 8, 18), scope, 1800).kind).toBe('fallback')
    const blank = page(); blank.data.fill(255)
    expect(composite.build(cover(blank, 8, 18), scope, 1800).kind).toBe('fallback')
    expect(composite.build({ ...original, width: 32 } as ImageData, scope, 1800).kind).toBe('fallback')
    expect(composite.build(cover(original, 8, 18), scope, 999).kind).toBe('fallback')
  })

  it('does not invent content when the same region stays occluded', () => {
    const original = page(), composite = new DocumentFrameComposite(), covered = cover(original, 8, 18)
    composite.add(covered, scope, 1000)
    expect(composite.build(covered, scope, 1200).kind).toBe('fallback')
  })

  it('never replaces a current red correction with older paper when color is ambiguous', () => {
    const previous = page(), current = page(), composite = new DocumentFrameComposite()
    const i = (24 * current.width + 20) * 4
    previous.data.set([240, 240, 240, 255], i)
    current.data.set([200, 50, 30, 255], i)
    composite.add(previous, scope, 1000)
    const result = composite.build(current, scope, 1800)
    expect(result.kind).toBe('fallback')
    expect(current.data.slice(i, i + 4)).toEqual(new Uint8ClampedArray([200, 50, 30, 255]))
  })

  it('keeps prior pixels explicitly unconfirmed when writing might have changed under the hand', () => {
    const original = page(), composite = new DocumentFrameComposite()
    composite.add(cover(original, 42, 52), scope, 1000)
    const modified = page()
    modified.data.set([15, 15, 15, 255], (22 * 64 + 12) * 4)
    const result = composite.build(cover(modified, 8, 18), scope, 1800)
    expect(result).toEqual({ kind: 'fallback', reason: 'unverified_occlusion_mask' })
  })

  it('removes all candidate frames on reset and caps memory', () => {
    const original = page(), composite = new DocumentFrameComposite()
    for (let i = 0; i < 20; i++) composite.add(cover(original, 42, 52), scope, 1000 + i * 100)
    expect(composite.retainedCount()).toBeLessThanOrEqual(3)
    composite.clear()
    expect(composite.retainedCount()).toBe(0)
    expect(composite.build(cover(original, 8, 18), scope, 3000).kind).toBe('fallback')
  })

  it('retains dimensions when ImageData properties live on the prototype', () => {
    const original = cover(page(), 42, 52)
    const browserLike = Object.create({ width: original.width, height: original.height, colorSpace: 'srgb' }) as ImageData
    Object.defineProperty(browserLike, 'data', { value: original.data })
    const composite = new DocumentFrameComposite()
    composite.add(browserLike, scope, 1000)
    expect(composite.build(cover(page(), 8, 18), scope, 1800)).toEqual({ kind: 'fallback', reason: 'unverified_occlusion_mask' })
  })
})
