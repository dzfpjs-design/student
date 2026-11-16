import { afterEach, describe, expect, it, vi } from 'vitest'
import { askTutorWithOcrEvidence, confirmOcrTutorEvidence, createOcrTutorReview, ocrTutorReviewMessage, parseOcrTutorReview, prepareOcrDocumentGoal, readOcrDocumentGoal, type OcrTutorReview } from './tutorOcrEvidence'
import type { OcrArtifact } from './types'
import { resetRuntimeCache } from './api'

const artifact = (overrides: Partial<OcrArtifact> = {}): OcrArtifact => ({
  artifact_id: 'artifact-question-1', extracted_text: '12 + 8 = ?', confidence: 0.93,
  local_path: null, status: 'completed',
  details: { lines: [], engine: 'local', ocr_confidence: 0.93, quality: { passed: true, issues: [], messages: [], metrics: {}, version: 'test' } },
  ...overrides
})

afterEach(() => { resetRuntimeCache(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers() })

describe('original document goal handoff', () => {
  const savedReview = () => ({ root_turn_id: 'original', child_id: 'child', session_id: 'session', revision: 2,
    phase: 'awaiting_confirmation', local_only: true, cloud_transmission_allowed: false, mastery_update_allowed: false,
    review: createOcrTutorReview(artifact()) })
  it.each(['transport', 'body'])('releases a hung original %s read and rejects its late result without a write', async stage => {
    vi.useFakeTimers()
    let finish!: (value: any) => void
    let signal!: AbortSignal
    const fetcher = vi.fn((_url, init) => {
      signal = init.signal
      const pending = new Promise(resolve => { finish = resolve })
      return stage === 'transport' ? pending : Promise.resolve({ ok: true, json: () => pending })
    })
    vi.stubGlobal('fetch', fetcher)
    let published = false
    const result = readOcrDocumentGoal('session', 'child', { rootTurnId: 'original', revision: 2 }).then(value => { published = true; return value })
    const rejected = expect(result).rejects.toMatchObject({ code: 'VOICE_RECEIPT_CHECK_TIMEOUT' })
    await vi.advanceTimersByTimeAsync(10_000)
    await rejected
    expect(signal.aborted).toBe(true)
    finish(stage === 'transport' ? { ok: true, json: async () => savedReview() } : savedReview())
    await vi.advanceTimersByTimeAsync(1)
    expect(published).toBe(false)
    expect(fetcher).toHaveBeenCalledOnce()
    expect(fetcher.mock.calls[0][1].method ?? 'GET').toBe('GET')
    expect(vi.getTimerCount()).toBe(0)
  })
  it('stops a hung original read immediately on privacy pause', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn(() => new Promise(() => {}))
    vi.stubGlobal('fetch', fetcher)
    const rejected = expect(readOcrDocumentGoal('session', 'child', { rootTurnId: 'original', revision: 2 })).rejects.toMatchObject({ name: 'AbortError' })
    await vi.advanceTimersByTimeAsync(1)
    window.dispatchEvent(new Event('hsc-privacy-pause'))
    await vi.advanceTimersByTimeAsync(1)
    await rejected
    expect(fetcher).toHaveBeenCalledOnce()
    expect(vi.getTimerCount()).toBe(0)
  })
  it('does not send an original read after its owner stops while runtime configuration is pending', async () => {
    vi.useFakeTimers()
    let finish!: (value: unknown) => void
    const config = vi.fn(() => new Promise(resolve => { finish = resolve }))
    vi.stubGlobal('desktop', { getRuntimeConfig: config })
    const fetcher = vi.fn()
    vi.stubGlobal('fetch', fetcher)
    const owner = new AbortController()
    const rejected = expect(readOcrDocumentGoal('session', 'child', { rootTurnId: 'original', revision: 2 }, owner.signal))
      .rejects.toMatchObject({ name: 'AbortError' })
    await vi.advanceTimersByTimeAsync(1)
    expect(config).toHaveBeenCalledOnce()
    owner.abort()
    await rejected
    finish({ status: 'ready', localApiUrl: 'http://127.0.0.1:9877', sessionToken: 'synthetic-only' })
    await vi.advanceTimersByTimeAsync(1)
    expect(fetcher).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })
  it('rejects an original review returned by a retired runtime', async () => {
    vi.useFakeTimers()
    let finish!: (value: unknown) => void
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: () => new Promise(resolve => { finish = resolve }) })))
    const rejected = expect(readOcrDocumentGoal('session', 'child', { rootTurnId: 'original', revision: 2 }))
      .rejects.toMatchObject({ code: 'LOCAL_SERVICE_CHANGED' })
    await vi.advanceTimersByTimeAsync(1)
    resetRuntimeCache()
    finish(savedReview())
    await rejected
    expect(vi.getTimerCount()).toBe(0)
  })
  it('reads the durable original review without capturing, confirming, or refreshing its source date', async () => {
    const saved = savedReview()
    saved.review.createdAt = '2025-01-01T10:00:00+08:00'
    const fetcher = vi.fn(async () => ({ ok: true, json: async () => saved }))
    vi.stubGlobal('fetch', fetcher)
    expect(await readOcrDocumentGoal('session', 'child', { rootTurnId: 'original', revision: 2 })).toEqual(saved.review)
    expect(fetcher).toHaveBeenCalledOnce()
    expect(fetcher).toHaveBeenCalledWith(expect.stringContaining('/document-goals/original/review'), expect.objectContaining({ method: 'GET', cache: 'no-store' }))
    expect(parseOcrTutorReview(saved.review)).toBeNull()
  })
  it.each(['root_turn_id', 'child_id', 'session_id', 'revision', 'phase', 'local_only', 'cloud_transmission_allowed', 'mastery_update_allowed'])('does not restore a changed %s', async key => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ ...savedReview(), [key]: 'other' }) })))
    await expect(readOcrDocumentGoal('session', 'child', { rootTurnId: 'original', revision: 2 })).rejects.toMatchObject({ code: 'DOCUMENT_GOAL_REVIEW_CHANGED' })
  })
  const confirmed = (): OcrTutorReview => ({ ...createOcrTutorReview(artifact()), status: 'confirmed', confirmationId: 'original-confirmation' })
  it.each(['connection', 'body', 'invalid_body'])('keeps a %s failure after Tutor submission as unknown, not a safe new request', async failure => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      if (failure === 'connection') throw Error('response lost')
      return { ok: true, json: async () => { if (failure === 'body') throw Error('truncated response'); return {} } }
    }))
    await expect(askTutorWithOcrEvidence('session', '数学', 'original-confirmation', 'original-action')).rejects.toMatchObject({ code: 'TUTOR_RESULT_UNCONFIRMED' })
  })
  it('sends only the exact saved confirmation and expected revision to the original goal', async () => {
    const fetcher = vi.fn(async () => ({ ok: true, json: async () => ({ status: 'ready', document_goal: {
      root_turn_id: 'original', session_id: 'session', artifact_id: 'artifact-question-1', confirmation_id: 'original-confirmation' } }) }))
    vi.stubGlobal('fetch', fetcher)
    await prepareOcrDocumentGoal('session', confirmed(), { rootTurnId: 'original', revision: 1 })
    expect(fetcher).toHaveBeenCalledWith(expect.stringContaining('/document-goals/original/confirm'), expect.objectContaining({ body: JSON.stringify({ confirmation_id: 'original-confirmation', revision: 1 }) }))
  })
  it.each(['root_turn_id', 'session_id', 'artifact_id', 'confirmation_id'])('rejects changed %s instead of falling back to an unbound Tutor', async key => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ status: 'ready', document_goal: {
      root_turn_id: 'original', session_id: 'session', artifact_id: 'artifact-question-1', confirmation_id: 'original-confirmation', [key]: 'other' } }) })))
    await expect(prepareOcrDocumentGoal('session', confirmed(), { rootTurnId: 'original', revision: 1 })).rejects.toMatchObject({ code: 'DOCUMENT_GOAL_RESULT_UNCONFIRMED' })
  })
  it('preserves a structured local conflict rather than hiding its cause', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 409, json: async () => ({ detail: { code: 'DOCUMENT_GOAL_SOURCE_CHANGED', message: '原题已变化' } }) })))
    await expect(prepareOcrDocumentGoal('session', confirmed(), { rootTurnId: 'original', revision: 1 })).rejects.toMatchObject({ code: 'DOCUMENT_GOAL_SOURCE_CHANGED', message: '原题已变化' })
  })
  it('treats a spoken correction of clear OCR as a manual correction', async () => {
    const fetcher = vi.fn(async () => ({ ok: true, json: async () => ({ confirmation_id: 'corrected', confirmed_text: '12+9=?', confirmation_kind: 'manual_correction' }) }))
    vi.stubGlobal('fetch', fetcher)
    await confirmOcrTutorEvidence('session', createOcrTutorReview(artifact()), '12+9=?')
    expect(fetcher).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ body: expect.stringContaining('"confirmation_kind":"manual_correction"') }))
  })
})

describe('OCR tutor evidence policy', () => {
  it('requires confirmation even for a locally clear OCR result', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-28T10:00:00Z'))
    expect(createOcrTutorReview(artifact()).status).toBe('needs_confirmation')
    expect(createOcrTutorReview(artifact({ confidence: 0.49, status: 'quality_warning' })).status).toBe('uncertain')
    vi.useRealTimers()
  })

  it('reviews the strongest red-marked question instead of the entire worksheet', () => {
    const review = createOcrTutorReview(artifact({
      extracted_text: '1. 第一题\n2. 第二题\n3. 第三题',
      status: 'quality_warning',
      details: {
        ...artifact().details,
        local_pipeline: {
          engine: 'local', route: 'cpu', quality: { passed: false, issues: ['possible_double_page_manual_split_required'], metrics: {} },
          question_blocks: [], manual_review: { required: true, reasons: [], handwriting: 'not inferred', math: 'manual' },
          queue: { task_id: 'ocr-task', behavior_fps_cap: 2 },
          wrong_question_candidates: [
            { text: '2. 第二题', confidence: .91, ocr_confidence: .9, bbox_xywh: [0, 20, 100, 20], red_pixel_count: 80, red_mark_count: 1, evidence: 'red_correction_mark_near_question', requires_child_confirmation: true }
          ]
        }
      }
    }))

    expect(review.text).toBe('2. 第二题')
    expect(review.source).toBe('marked_question')
    expect(review.status).toBe('needs_confirmation')
  })

  it('uses a cloud page candidate instead of low-quality local gibberish but keeps it uncertain', () => {
    const review = createOcrTutorReview(artifact({
      extracted_text: '1. 错乱本地字串', confidence: .41, status: 'quality_warning',
      details: {
        ...artifact().details,
        quality: { passed: false, issues: ['blurred'], messages: [], metrics: {}, version: 'test' },
        cloud_vision_fallback: {
          status: 'needs_confirmation',
          blocks: [{ kind: 'text', text: '1. 计算 125 × 32。', confidence: .69 }],
          privacy: { uploaded: 'page_region_binary_ink', original_frame_uploaded: false },
        },
      },
    }))

    expect(review).toMatchObject({ text: '1. 计算 125 × 32。', source: 'cloud_page', status: 'uncertain' })
    expect(ocrTutorReviewMessage(review)).toContain('确认前不会讲解、判错或更新掌握度')
  })

  it('keeps a reliable local reading instead of replacing it with cloud text', () => {
    const review = createOcrTutorReview(artifact({
      details: {
        ...artifact().details,
        cloud_vision_fallback: {
          status: 'needs_confirmation',
          blocks: [{ kind: 'text', text: '云端候选', confidence: .69 }],
        },
      },
    }))
    expect(review).toMatchObject({ text: '12 + 8 = ?', source: 'full_page', status: 'needs_confirmation' })
  })

  it('restores only an unexpired, server-confirmed local review shape', () => {
    const now = new Date().toISOString()
    expect(parseOcrTutorReview({ artifactId: 'artifact-question-1', text: '12 + 8 = ?', status: 'confirmed', confirmationId: 'confirmation-1', confirmationKind: 'ocr_verified', createdAt: now, confirmedAt: now, expiresAt: new Date(Date.now() + 60 * 60_000).toISOString() })).not.toBeNull()
    expect(parseOcrTutorReview({ artifactId: '', text: '12 + 8 = ?', status: 'confirmed', createdAt: now })).toBeNull()
    expect(parseOcrTutorReview({ artifactId: 'artifact-question-1', text: '12 + 8 = ?', status: 'confirmed', confirmationId: 'confirmation-old', confirmationKind: 'ocr_verified', createdAt: new Date(Date.now() - 25 * 60 * 60_000).toISOString(), confirmedAt: now, expiresAt: new Date(Date.now() + 60 * 60_000).toISOString() })).toBeNull()
  })
})
