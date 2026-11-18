/**
 * Short-lived, non-authoritative candidate assembly for a single camera scope.
 * The result is NOT an original photograph: a covered area may have changed
 * after the source frame was taken, so every candidate requires verification.
 */
type Frame = { image: ImageData; scope: string; at: number }
type CompositeResult = { kind: 'fallback'; reason?: 'unverified_occlusion_mask' } | { kind: 'candidate'; image: ImageData; filledPixels: number; requiresReview: true }

const MAX_PIXELS = 1280 * 960
const MAX_FRAMES = 3
const MAX_AGE_MS = 5_000

function isSkin(data: Uint8ClampedArray, index: number): boolean {
  const r = data[index], g = data[index + 1], b = data[index + 2]
  return r > 70 && g > 35 && b > 20 && r > g * 1.08 && r > b * 1.18 && Math.max(r, g, b) - Math.min(r, g, b) > 18
}

function valid(image: ImageData): boolean {
  return Number.isSafeInteger(image.width) && Number.isSafeInteger(image.height)
    && image.width >= 32 && image.height >= 32 && image.width * image.height <= MAX_PIXELS
    && image.data.length === image.width * image.height * 4
}

/** Accept only a close pixel registration with independent ink landmarks in
 * all four quadrants. Translation/perspective/ambiguous scenes fail closed. */
function samePage(previous: ImageData, current: ImageData): boolean {
  if (previous.width !== current.width || previous.height !== current.height) return false
  const { width, height } = current
  const step = Math.max(1, Math.floor(Math.min(width, height) / 64))
  let samples = 0, matches = 0
  const landmarks = [0, 0, 0, 0]
  for (let y = 0; y < height; y += step) for (let x = 0; x < width; x += step) {
    const i = (y * width + x) * 4
    if (isSkin(previous.data, i) || isSkin(current.data, i)) continue
    const p = (previous.data[i] * 3 + previous.data[i + 1] * 6 + previous.data[i + 2]) / 10
    const c = (current.data[i] * 3 + current.data[i + 1] * 6 + current.data[i + 2]) / 10
    samples++
    if (Math.abs(p - c) <= 18) {
      matches++
      if (p < 100 && c < 100) landmarks[(y >= height / 2 ? 2 : 0) + (x >= width / 2 ? 1 : 0)]++
    }
  }
  const gridPoints = Math.ceil(width / step) * Math.ceil(height / step)
  return samples >= gridPoints * 0.6 && matches / samples >= 0.94 && landmarks.every((count) => count >= 5)
}

export class DocumentFrameComposite {
  private frames: Frame[] = []

  add(image: ImageData, scope: string, at: number): void {
    if (!scope || !Number.isFinite(at) || !valid(image)) { this.clear(); return }
    if (this.frames.some((frame) => frame.scope !== scope || frame.at > at)) this.clear()
    this.frames = this.frames.filter((frame) => at - frame.at <= MAX_AGE_MS)
    // Clone; callers may immediately reuse the live canvas buffer.
    this.frames.push({ scope, at, image: {
      width: image.width, height: image.height, colorSpace: image.colorSpace,
      data: new Uint8ClampedArray(image.data),
    } as ImageData })
    if (this.frames.length > MAX_FRAMES) this.frames.shift()
  }

  build(current: ImageData, scope: string, at: number): CompositeResult {
    if (!valid(current) || !scope || !Number.isFinite(at)) return { kind: 'fallback' }
    if (this.frames.some((frame) => frame.scope !== scope || frame.at > at)) { this.clear(); return { kind: 'fallback' } }
    this.frames = this.frames.filter((frame) => at - frame.at <= MAX_AGE_MS)
    const sources = [...this.frames].reverse().filter((frame) => samePage(frame.image, current))
    if (!sources.length) return { kind: 'fallback' }
    for (let i = 0; i < current.data.length; i += 4) {
      if (!isSkin(current.data, i)) continue
      const source = sources.find((frame) => !isSkin(frame.image.data, i))
      if (!source) continue
      // RGB cannot establish whether a pixel is hand, red correction, ink,
      // or another important mark. Do not overwrite any current pixels until
      // an independently validated occlusion mask is available.
      return { kind: 'fallback', reason: 'unverified_occlusion_mask' }
    }
    return { kind: 'fallback' }
  }

  clear(): void { this.frames = [] }
  retainedCount(): number { return this.frames.length }
}
