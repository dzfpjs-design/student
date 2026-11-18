export type DocumentFrameState = 'no_page' | 'stabilizing' | 'writing' | 'occluded' | 'page_changed' | 'stable'

export type DocumentContentFingerprint = { width: number; height: number; pixels: Uint8Array }

export type DocumentFrameFeatures = {
  brightness: number
  contrast: number
  paperRatio: number
  skinRatio: number
  fingerprint: Uint8Array
  contentFingerprint?: DocumentContentFingerprint
}

export type DocumentFrameDecision = {
  state: DocumentFrameState
  confidence: number
  captureRecommended: boolean
  changed: boolean
  motionScore: number
  contentMotion: 'changed' | 'unchanged' | 'unavailable'
}

type Options = {
  stableSamples: number
  stableMotionThreshold: number
  minimumCaptureIntervalMs: number
  refreshCaptureIntervalMs: number
}

const DEFAULT_OPTIONS: Options = {
  stableSamples: 4,
  // USB document cameras commonly fluctuate by 2-4 luminance points between
  // identical frames because of auto exposure and MJPEG compression.
  stableMotionThreshold: 4.5,
  minimumCaptureIntervalMs: 15_000,
  refreshCaptureIntervalMs: 5 * 60_000
}

export class DocumentFrameMonitor {
  private readonly options: Options
  private previous: DocumentFrameFeatures | null = null
  private previousObservedAt: number | null = null
  private previousFrameToken: number | null = null
  private lastCapturedFingerprint: Uint8Array | null = null
  private lastCapturedContent: DocumentContentFingerprint | null = null
  private lastCaptureAt: number | null = null
  private lastAttemptAt: number | null = null
  private lastEarlyRetryAt: number | null = null
  private earlyRetryNotBefore: number | null = null
  private stableCount = 0
  private state: DocumentFrameState = 'stabilizing'

  constructor(options: Partial<Options> = {}) {
    this.options = { ...DEFAULT_OPTIONS, ...options }
  }

  /** A repeated decoded frame must not count as another stable observation. */
  observeFreshFrame(features: DocumentFrameFeatures, observedAt: number, frameToken: number | null): DocumentFrameDecision | null {
    if (frameToken === null || !Number.isSafeInteger(frameToken) || frameToken < 0) return null
    if (frameToken === this.previousFrameToken) return null
    if (this.previousFrameToken !== null && frameToken < this.previousFrameToken) {
      // A reset counter is a new observation sequence, not evidence that the
      // previously sampled picture stayed stable.
      this.previous = null
      this.previousObservedAt = null
      this.stableCount = 0
      this.state = 'stabilizing'
    }
    this.previousFrameToken = frameToken
    return this.observe(features, observedAt)
  }

  observe(features: DocumentFrameFeatures, observedAt: number): DocumentFrameDecision {
    const motionScore = this.previous ? fingerprintDistance(features.fingerprint, this.previous.fingerprint) : 0
    const capturedDifference = this.lastCapturedFingerprint
      ? fingerprintDistance(features.fingerprint, this.lastCapturedFingerprint)
      : Number.POSITIVE_INFINITY
    const contentComparable = Boolean(features.contentFingerprint && this.previous?.contentFingerprint)
    const contentMoving = contentChanged(features.contentFingerprint, this.previous?.contentFingerprint)
    let next: DocumentFrameState
    if (features.paperRatio < 0.1 || features.brightness < 28
      || (features.contrast < 7 && !hasLocalInk(features.contentFingerprint))) {
      next = 'no_page'
      this.stableCount = 0
    } else if (features.skinRatio >= 0.22) {
      next = 'occluded'
      this.stableCount = 0
    } else if (motionScore >= 7 && features.skinRatio >= 0.025) {
      next = 'writing'
      this.stableCount = 0
    } else if (motionScore >= 11) {
      next = this.lastCapturedFingerprint ? 'page_changed' : 'stabilizing'
      this.stableCount = 1
    } else if (contentMoving) {
      next = 'writing'
      this.stableCount = 0
    } else if (motionScore <= this.options.stableMotionThreshold && features.skinRatio < 0.1) {
      this.stableCount += 1
      next = this.stableCount >= this.options.stableSamples ? 'stable' : 'stabilizing'
    } else {
      this.stableCount = 0
      next = 'stabilizing'
    }
    const changed = next !== this.state
    this.state = next
    this.previous = { ...features, fingerprint: new Uint8Array(features.fingerprint),
      contentFingerprint: copyContent(features.contentFingerprint) }
    this.previousObservedAt = observedAt
    const elapsed = this.lastCaptureAt === null ? Number.POSITIVE_INFINITY : observedAt - this.lastCaptureAt
    const changedSinceCapture = this.lastCapturedFingerprint !== null && (
      features.contentFingerprint && this.lastCapturedContent
        ? contentChanged(features.contentFingerprint, this.lastCapturedContent)
        : capturedDifference >= 3.5)
    const captureRecommended = next === 'stable' && this.canCaptureAt(observedAt) && (
      this.lastCapturedFingerprint === null
      || changedSinceCapture
      || elapsed >= this.options.refreshCaptureIntervalMs
    )
    return {
      state: next,
      confidence: confidenceFor(next, features, motionScore),
      captureRecommended,
      changed,
      motionScore: Math.round(motionScore * 100) / 100,
      contentMotion: contentComparable ? (contentMoving ? 'changed' : 'unchanged') : 'unavailable'
    }
  }

  markCaptured(fingerprint: Uint8Array, capturedAt: number, contentFingerprint?: DocumentContentFingerprint): void {
    this.lastCapturedFingerprint = new Uint8Array(fingerprint)
    this.lastCapturedContent = copyContent(contentFingerprint) ?? null
    this.lastCaptureAt = capturedAt
    this.lastAttemptAt = capturedAt
  }

  matchesCapturedPage(features: DocumentFrameFeatures): boolean {
    // Compare a fresh sample without consuming the next sampling event/change.
    if (this.state !== 'stable' || !this.lastCapturedFingerprint
      || features.paperRatio < .1 || features.brightness < 28 || features.skinRatio >= .1
      || (features.contrast < 7 && !hasLocalInk(features.contentFingerprint))) return false
    if (Boolean(features.contentFingerprint) !== Boolean(this.lastCapturedContent)) return false
    return features.contentFingerprint && this.lastCapturedContent
      ? !contentChanged(features.contentFingerprint, this.lastCapturedContent)
      : fingerprintDistance(features.fingerprint, this.lastCapturedFingerprint) < 3.5
  }

  // Compare the exact frame about to be submitted with the latest stable
  // sampling frame, not the last OCR capture (which may predate the draft).
  matchesStableFrame(features: DocumentFrameFeatures, observedAt: number, frameToken: number | null, allowCurrentFrame = false): boolean {
    const previous = this.previous
    if (this.state !== 'stable' || !previous || this.previousObservedAt === null
      || !Number.isFinite(observedAt) || observedAt < this.previousObservedAt
      || observedAt - this.previousObservedAt > 1500
      || frameToken === null || !Number.isSafeInteger(frameToken)
      || this.previousFrameToken === null || frameToken < this.previousFrameToken
      || (!allowCurrentFrame && frameToken === this.previousFrameToken)
      || features.paperRatio < .1 || features.brightness < 28 || features.skinRatio >= .1
      || (features.contrast < 7 && !hasLocalInk(features.contentFingerprint))
      || !features.contentFingerprint || !previous.contentFingerprint) return false
    return fingerprintDistance(features.fingerprint, previous.fingerprint) <= this.options.stableMotionThreshold
      && !contentChanged(features.contentFingerprint, previous.contentFingerprint)
  }

  /** Discard stability evidence after a pause or an extended loss of frames. */
  invalidateStability(): void {
    this.previous = null
    this.previousObservedAt = null
    this.previousFrameToken = null
    this.stableCount = 0
    this.state = 'stabilizing'
  }

  canCaptureAt(observedAt: number): boolean {
    if (this.lastAttemptAt === null || observedAt - this.lastAttemptAt >= this.options.minimumCaptureIntervalMs) return true
    return this.earlyRetryNotBefore !== null && observedAt >= this.earlyRetryNotBefore
      && (this.lastEarlyRetryAt === null || observedAt - this.lastEarlyRetryAt >= 60_000)
  }

  markCaptureAttempt(attemptedAt: number): void {
    if (this.lastAttemptAt !== null && attemptedAt - this.lastAttemptAt < this.options.minimumCaptureIntervalMs) {
      this.lastEarlyRetryAt = attemptedAt
    }
    this.lastAttemptAt = attemptedAt
    this.earlyRetryNotBefore = null
  }

  /** Allow a new stable frame after interrupted OCR; never accept the old result. */
  markStaleCaptureAttempt(observedAt: number): void {
    if (this.lastAttemptAt === null || !Number.isFinite(observedAt) || observedAt < this.lastAttemptAt
      || observedAt - this.lastAttemptAt >= this.options.minimumCaptureIntervalMs) return
    this.earlyRetryNotBefore = this.lastAttemptAt + Math.min(5_000, this.options.minimumCaptureIntervalMs)
  }

  reset(): void {
    this.previous = null
    this.previousObservedAt = null
    this.previousFrameToken = null
    this.lastCapturedFingerprint = null
    this.lastCapturedContent = null
    this.lastCaptureAt = null
    // Preserve the last attempt across device resets within this component.
    this.earlyRetryNotBefore = null
    this.stableCount = 0
    this.state = 'stabilizing'
  }
}

/** Coarse, closed diagnostics: never persist pixels, fingerprints or OCR text. */
export function documentFrameDiagnosticBands(decision: DocumentFrameDecision, features: DocumentFrameFeatures) {
  const motion = decision.motionScore
  const skin = features.skinRatio
  return {
    frame_motion_band: motion < 2 ? 'lt_2' : motion <= 4.5 ? '2_to_4_5' : motion < 7 ? '4_5_to_7' : motion < 11 ? '7_to_11' : 'ge_11',
    frame_skin_band: skin < .025 ? 'lt_025' : skin < .1 ? '025_to_10' : skin < .22 ? '10_to_22' : 'ge_22',
    frame_content_motion: decision.contentMotion,
    frame_paper_band: features.paperRatio < .1 ? 'lt_10' : 'ge_10',
    frame_light_band: features.brightness < 28 ? 'dark' : 'usable',
  }
}

export function extractDocumentFrameFeatures(image: Pick<ImageData, 'data' | 'width' | 'height'>): DocumentFrameFeatures {
  const rows = 6
  const columns = 8
  const fingerprint = new Uint8Array(rows * columns)
  const cellLuminance = new Float64Array(rows * columns)
  const cellSamples = new Uint32Array(rows * columns)
  const contentPixels = new Uint8Array(image.width * image.height)
  let luminanceSum = 0
  let luminanceSquared = 0
  let paper = 0
  let skin = 0
  let samples = 0
  // Cell centers can all land between lines of handwriting. Measure each
  // cell's area so a visible page and small exposure changes remain stable.
  for (let y = 0; y < image.height; y += 1) for (let x = 0; x < image.width; x += 1) {
    const cell = Math.floor(y * rows / image.height) * columns + Math.floor(x * columns / image.width)
    const offset = (y * image.width + x) * 4
    const red = image.data[offset]
    const green = image.data[offset + 1]
    const blue = image.data[offset + 2]
    const luminance = red * 0.2126 + green * 0.7152 + blue * 0.0722
    cellLuminance[cell] += luminance
    cellSamples[cell] += 1
    contentPixels[y * image.width + x] = Math.round(luminance)
    luminanceSum += luminance
    luminanceSquared += luminance * luminance
    if (luminance >= 135 && Math.max(red, green, blue) - Math.min(red, green, blue) <= 70) paper += 1
    if (red > 70 && green > 35 && blue > 20 && red > green * 1.08 && red > blue * 1.18 && Math.max(red, green, blue) - Math.min(red, green, blue) > 18) skin += 1
    samples += 1
  }
  for (let cell = 0; cell < fingerprint.length; cell += 1) {
    fingerprint[cell] = cellSamples[cell] ? Math.round(cellLuminance[cell] / cellSamples[cell]) : 0
  }
  const brightness = luminanceSum / samples
  // Keep pixel positions: a minus sign or a 2-to-5 correction can have the
  // same regional mean. This normalized map stays in RAM, never in events.
  for (let pixel = 0; pixel < contentPixels.length; pixel += 1) {
    contentPixels[pixel] = Math.min(255, Math.round(contentPixels[pixel] / Math.max(1, brightness) * 128))
  }
  // Suppress isolated sensor/compression pixels while retaining a 2x2 decimal
  // point. A small box filter also limits sensitivity to subpixel resampling.
  const smoothedContent = contentPixels.slice()
  for (let y = 1; y < image.height - 1; y += 1) for (let x = 1; x < image.width - 1; x += 1) {
    let total = 0
    for (let dy = -1; dy <= 1; dy += 1) for (let dx = -1; dx <= 1; dx += 1) {
      total += contentPixels[(y + dy) * image.width + x + dx]
    }
    smoothedContent[y * image.width + x] = Math.round(total / 9)
  }
  return {
    brightness: Math.round(brightness * 10) / 10,
    contrast: Math.round(Math.sqrt(Math.max(0, luminanceSquared / samples - brightness * brightness)) * 10) / 10,
    paperRatio: paper / samples,
    skinRatio: skin / samples,
    fingerprint,
    contentFingerprint: { width: image.width, height: image.height, pixels: smoothedContent }
  }
}

export function fingerprintSignature(fingerprint: Uint8Array): string {
  return Array.from(fingerprint, (value) => value.toString(16).padStart(2, '0')).join('')
}

function fingerprintDistance(left: Uint8Array, right: Uint8Array): number {
  const length = Math.min(left.length, right.length)
  if (!length) return 0
  let total = 0
  for (let index = 0; index < length; index += 1) total += Math.abs(left[index] - right[index])
  return total / length
}

function copyContent(value?: DocumentContentFingerprint): DocumentContentFingerprint | undefined {
  return value ? { ...value, pixels: value.pixels.slice() } : undefined
}

function hasLocalInk(value?: DocumentContentFingerprint): boolean {
  if (!value) return false
  let ink = 0
  for (const pixel of value.pixels) if (pixel < 100 && ++ink >= 4) return true
  return false
}

function hasDistributedAlignmentAnchors(value: DocumentContentFingerprint): boolean {
  const { width, height, pixels } = value
  // A lone mark on an otherwise blank page cannot establish that the whole
  // page moved. Require visible edges in several separated regions before
  // accepting a translated frame as the same page.
  const regions = new Set<number>()
  for (let y = 2; y < height - 2; y += 2) for (let x = 2; x < width - 2; x += 2) {
    const pixel = pixels[y * width + x]
    if (Math.abs(pixel - pixels[y * width + x + 2]) >= 20 || Math.abs(pixel - pixels[(y + 2) * width + x]) >= 20) {
      regions.add(Math.floor(y * 6 / height) * 8 + Math.floor(x * 8 / width))
      if (regions.size >= 3) return true
    }
  }
  return false
}

function contentChanged(left?: DocumentContentFingerprint | null, right?: DocumentContentFingerprint | null): boolean {
  if (!left || !right) return false
  if (left.width !== right.width || left.height !== right.height) return true
  const { width, height } = left
  // Focus and video scaling can move a stationary page by two sample pixels.
  // Align the entire page before attributing a local difference to new ink;
  // this does not merge frames or hide actual, non-translational edits.
  const shifts = width > 4 && height > 4
    ? [[0, 0], [-1, 0], [1, 0], [0, -1], [0, 1], [-1, -1], [1, 1], [-1, 1], [1, -1],
      [-2, 0], [2, 0], [0, -2], [0, 2], [-2, -1], [-2, 1], [2, -1], [2, 1],
      [-1, -2], [1, -2], [-1, 2], [1, 2], [-2, -2], [-2, 2], [2, -2], [2, 2]] : [[0, 0]]
  let alignmentAnchored: boolean | undefined
  for (const [dx, dy] of shifts) {
    if (dx !== 0 || dy !== 0) {
      if (alignmentAnchored === undefined) alignmentAnchored = hasDistributedAlignmentAnchors(left)
      if (!alignmentAnchored) return true
    }
    let changedPixels = 0
    // Compare every pixel with a corresponding pixel in the translated image.
    // Only the strip exposed by a nonzero translation lacks a counterpart;
    // discarding a fixed border would also hide real ink there at zero shift.
    const xStart = Math.max(0, -dx), xEnd = Math.min(width, width - dx)
    const yStart = Math.max(0, -dy), yEnd = Math.min(height, height - dy)
    for (let y = yStart; y < yEnd && changedPixels < 4; y += 1) {
      for (let x = xStart; x < xEnd; x += 1) {
        if (Math.abs(left.pixels[y * width + x] - right.pixels[(y + dy) * width + x + dx]) >= 24
          && ++changedPixels >= 4) break
      }
    }
    if (changedPixels < 4) return false
  }
  return true
}

function confidenceFor(state: DocumentFrameState, features: DocumentFrameFeatures, motion: number): number {
  if (state === 'stable') return Math.min(0.98, 0.72 + features.paperRatio * 0.22)
  if (state === 'writing') return Math.min(0.95, 0.55 + features.skinRatio + motion / 100)
  if (state === 'occluded') return Math.min(0.98, 0.65 + features.skinRatio)
  if (state === 'no_page') return Math.max(0, Math.min(0.96, 0.65 + (0.1 - features.paperRatio) * 2))
  if (state === 'page_changed') return Math.min(0.94, 0.62 + motion / 80)
  return 0.55
}
