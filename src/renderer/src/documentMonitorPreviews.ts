import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import { ApiError, localApi } from './api'
import type { DocumentInspectionOutput } from './cameraDeviceRuntime'
import { contentChanged, extractDocumentFrameFeatures, type DocumentFrameFeatures } from './documentFrameMonitor'
import type { DocumentOcrPreview, DocumentOcrPreviewPhase } from './types'
import { DocumentPreviewPageEpoch } from './documentPreviewPageEpoch'

const sampleCount = 4
const minimumSpan = 1_500
const maximumGap = 1_250
const maximumMotion = 1
const minimumAttemptInterval = 20_000
const refreshAttemptInterval = 5 * 60_000
const previewPixelChange = 12
type PreviewDiagnostic = 'started' | 'saved' | 'unconfirmed' | 'resolve_saved' | 'resolve_retired' | 'resolve_source_blocked' | 'refresh_due'
const previewErrorCodes = new Set(['DOCUMENT_PREVIEW_SOURCE_CHANGED', 'DOCUMENT_PREVIEW_ACTION_CONFLICT',
  'DOCUMENT_PREVIEW_RETIRED', 'DOCUMENT_PREVIEW_OCR_FAILED', 'DOCUMENT_PREVIEW_RESOLUTION_MISMATCH', 'DOCUMENT_OCR_BUSY'])

function previewDiagnostic(event: PreviewDiagnostic, startedAt: number, count?: number, error?: unknown): void {
  const details: { latency_ms: number; count?: number; error_code?: string } = {
    latency_ms: Math.max(0, Math.min(300_000, Date.now() - startedAt))
  }
  if (count !== undefined && Number.isFinite(count)) details.count = Math.max(0, Math.min(10_000, Math.floor(count)))
  if (error !== undefined) {
    const code = error instanceof ApiError ? error.code : error instanceof Error ? error.message : undefined
    details.error_code = code && previewErrorCodes.has(code) ? code : 'DOCUMENT_PREVIEW_RESULT_UNCONFIRMED'
  }
  try { void window.desktop?.recordRendererDiagnostic?.({ event: `document.preview.${event}`,
    level: event === 'unconfirmed' || event === 'resolve_source_blocked' ? 'warn' : 'info', details })?.catch(() => undefined) }
  catch { /* Diagnostics cannot interrupt recognition or recovery. */ }
}

type PreviewSample = {
  video: HTMLVideoElement
  features: DocumentFrameFeatures
  motion: number
  token: number
  generation: number
  leaseId: string
  cameraKey: string
  current: () => boolean
  available: boolean
  allowClear?: boolean
  pageChanged?: boolean
  inspection?: {
    turnId: string
    report: (result: Extract<DocumentInspectionOutput, { page_status: 'partial_local_preview' }>) => void
    unavailable: () => void
  }
}
type Warmth = 'clear' | 'warm' | 'high'
type PreviewAttempt = {
  scope: string
  actionId?: string
  generation?: number
  leaseId?: string
  phase?: DocumentOcrPreviewPhase
  pageToken?: string
  inspectionTurnId?: string
  at: number
  frame: DocumentFrameFeatures
  state: 'pending' | 'saved' | 'interrupted' | 'unconfirmed'
}
type SampleWindow = {
  cameraKey: string
  inspectionTurnId?: string
  warmth: Warmth
  changedSinceAttempt: boolean
  reference: PreviewAttempt | null
  token: number
  firstAt: number
  lastAt: number
  count: number
  anchor: Uint8Array
}
type PendingPreview = {
  controller: AbortController
  cameraKey: string
  inspectionTurnId?: string
  warmth: Warmth
  frame: DocumentFrameFeatures
  attempt?: PreviewAttempt
}
type PreviewHistory = { scope: string | null; items: DocumentOcrPreview[]; loaded: boolean; unavailable: boolean }
const emptyHistory: PreviewHistory = { scope: null, items: [], loaded: false, unavailable: false }
type CaptureStatus = 'idle' | 'reading' | 'interrupted' | 'unconfirmed'

function previewWarmth(features: DocumentFrameFeatures, motion: number, allowClear = false): Warmth | null {
  if (features.paperRatio < .10 || features.brightness < 28 || features.contrast < 7 || motion > maximumMotion) return null
  if (features.skinRatio < .10) return allowClear ? 'clear' : null
  if (features.skinRatio < .22) return 'warm'
  // Color alone cannot tell a stationary hand from a warm backdrop. Require
  // a stronger paper/contrast signal and more settled frames for a partial read.
  return features.skinRatio < .65 && features.paperRatio >= .20 && features.contrast >= 16 ? 'high' : null
}

export function isWarmLocalPreviewCandidate(features: DocumentFrameFeatures, motion: number): boolean {
  return previewWarmth(features, motion) !== null
}

function coarseDistance(left: Uint8Array, right: Uint8Array): number {
  if (!left.length || left.length !== right.length) return Infinity
  return left.reduce((sum, value, index) => sum + Math.abs(value - right[index]), 0) / left.length
}

function previewContentChanged(previous: DocumentFrameFeatures, current: DocumentFrameFeatures): boolean {
  return coarseDistance(previous.fingerprint, current.fingerprint) > maximumMotion
    || contentChanged(previous.contentFingerprint, current.contentFingerprint, previewPixelChange)
}

async function frameSignature(image: string): Promise<string | undefined> {
  if (!crypto.subtle) return
  // Only exact encoded bytes can suppress a new read after restart. A coarse
  // layout hash can miss a new digit on a freshly reopened paper.
  try {
    const bytes = Uint8Array.from(atob(image), value => value.charCodeAt(0))
    const digest = await crypto.subtle.digest('SHA-256', bytes)
    return Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('')
  } catch {
    return undefined
  }
}

function frozenPreviewImage(video: HTMLVideoElement, observed: DocumentFrameFeatures, warmth: Warmth) {
  // Inspect the exact frozen frame sent to the local reader, not another live
  // preview sample. Keep full framing and bound the OCR pixel count.
  const snapshot = document.createElement('canvas')
  const scale = Math.min(1, Math.sqrt((4160 * 3120) / (video.videoWidth * video.videoHeight)))
  snapshot.width = Math.floor(video.videoWidth * scale)
  snapshot.height = Math.floor(video.videoHeight * scale)
  if (snapshot.width < 320 || snapshot.height < 1) return
  const context = snapshot.getContext('2d')
  if (!context) return
  context.drawImage(video, 0, 0, snapshot.width, snapshot.height)
  const sampled = document.createElement('canvas')
  sampled.width = 320
  sampled.height = Math.max(180, Math.round(video.videoHeight / video.videoWidth * sampled.width))
  const sampler = sampled.getContext('2d', { willReadFrequently: true })
  if (!sampler) return
  sampler.drawImage(snapshot, 0, 0, sampled.width, sampled.height)
  const features = extractDocumentFrameFeatures(sampler.getImageData(0, 0, sampled.width, sampled.height))
  if (previewWarmth(features, coarseDistance(features.fingerprint, observed.fingerprint), warmth === 'clear') !== warmth) return
  const image = snapshot.toDataURL('image/jpeg', .82).split(',', 2)[1]
  return image ? { image, features } : undefined
}

export function useDocumentMonitorPreviews(sessionId: string, childId: string | undefined, phase: DocumentOcrPreviewPhase | undefined, enabled: boolean) {
  const scope = enabled && childId && phase ? JSON.stringify([sessionId, childId, phase]) : null
  const ownerRef = useRef<{ scope: string } | null>(null)
  const windowRef = useRef<SampleWindow | null>(null)
  const activeRead = useRef<AbortController | null>(null)
  const pending = useRef<PendingPreview | null>(null)
  const attempts = useRef<PreviewAttempt | null>(null)
  const unresolved = useRef<PreviewAttempt | null>(null)
  const resolution = useRef<{ attempt: PreviewAttempt; controller: AbortController } | null>(null)
  const resolutionRetryAt = useRef(0)
  const historyWriteVersion = useRef(0)
  const pageEpoch = useRef(new DocumentPreviewPageEpoch())
  const pageTokenRef = useRef<string | undefined>(undefined)
  const busyRetryAt = useRef(0)
  const retiredRetryAt = useRef(0)
  const [history, setHistory] = useState(emptyHistory)
  const [captureStatus, setCaptureStatus] = useState<CaptureStatus>('idle')

  const cancelFramePreview = useCallback(() => {
    windowRef.current = null
    const current = pending.current
    if (current && !current.controller.signal.aborted) {
      current.controller.abort()
      if (current.attempt?.state === 'pending') {
        current.attempt.state = 'interrupted'
        unresolved.current = current.attempt
        previewDiagnostic('unconfirmed', current.attempt.at, undefined, new Error('DOCUMENT_PREVIEW_RESULT_UNCONFIRMED'))
        if (ownerRef.current) setCaptureStatus('interrupted')
      }
    }
  }, [])

  const invalidatePage = useCallback(() => {
    cancelFramePreview()
    pageEpoch.current.reset()
    pageTokenRef.current = undefined
    attempts.current = null
    busyRetryAt.current = 0
    retiredRetryAt.current = 0
  }, [cancelFramePreview])

  const resolveAttempt = useCallback(() => {
    const owner = ownerRef.current, attempt = unresolved.current
    if (!owner || owner.scope !== scope || !attempt || attempt.scope !== owner.scope || !attempt.actionId
        || attempt.generation === undefined || !attempt.leaseId || !attempt.phase || !attempt.pageToken
        || resolution.current || Date.now() < resolutionRetryAt.current) return
    const controller = new AbortController()
    const active = { attempt, controller }
    const startedAt = Date.now()
    resolution.current = active
    void localApi.resolveDocumentMonitorPreview(sessionId, attempt.actionId, attempt.generation, attempt.leaseId,
      attempt.phase, attempt.pageToken, controller.signal, attempt.inspectionTurnId).then(result => {
      if (ownerRef.current !== owner || controller.signal.aborted || unresolved.current !== attempt) return
      if (result.client_action_id !== attempt.actionId || result.generation !== attempt.generation || result.phase !== attempt.phase
          || (result.status !== 'saved' && result.status !== 'retired')) throw new Error('DOCUMENT_PREVIEW_RESOLUTION_MISMATCH')
      if (result.status === 'saved') {
        const preview = result.preview
        if (!preview?.preview_id || preview.client_action_id !== attempt.actionId || preview.generation !== attempt.generation
            || preview.phase !== attempt.phase || preview.page_draft && preview.page_draft.page_token !== attempt.pageToken) {
          throw new Error('DOCUMENT_PREVIEW_RESOLUTION_MISMATCH')
        }
        attempt.state = 'saved'
        previewDiagnostic('resolve_saved', startedAt, preview.question_count)
        historyWriteVersion.current += 1
        setHistory(previous => previous.scope === owner.scope ? ({ ...previous,
          items: [preview, ...previous.items.filter(item => item.preview_id !== preview.preview_id)].slice(0, 10)
        }) : previous)
      } else {
        // Retirement is an atomic server receipt: even a late original POST
        // can no longer commit. An empty recent-history list cannot prove this.
        if (attempts.current === attempt) attempts.current = null
        retiredRetryAt.current = attempt.at + minimumAttemptInterval
        previewDiagnostic('resolve_retired', startedAt)
      }
      unresolved.current = null
      resolutionRetryAt.current = 0
      windowRef.current = null
      setCaptureStatus('idle')
    }).catch(error => {
      if (ownerRef.current !== owner || controller.signal.aborted || unresolved.current !== attempt) return
      attempt.state = 'unconfirmed'
      setCaptureStatus('unconfirmed')
      // A changed privacy/source scope needs a fresh owner; never silently
      // replay an image after the server rejects that captured scope.
      resolutionRetryAt.current = error instanceof ApiError && [401, 403, 409].includes(error.status ?? 0)
        ? Infinity : Date.now() + minimumAttemptInterval
      if (resolutionRetryAt.current === Infinity) previewDiagnostic('resolve_source_blocked', startedAt, undefined, error)
    }).finally(() => {
      if (resolution.current === active) resolution.current = null
    })
  }, [scope, sessionId])

  const refresh = useCallback(() => {
    const owner = ownerRef.current
    if (!owner || owner.scope !== scope) return
    activeRead.current?.abort()
    const controller = new AbortController()
    const readVersion = historyWriteVersion.current
    activeRead.current = controller
    setHistory(previous => ({ scope: owner.scope, items: previous.scope === scope ? previous.items : [], loaded: false, unavailable: false }))
    void localApi.documentMonitorPreviews(sessionId, controller.signal).then(result => {
      if (ownerRef.current === owner && !controller.signal.aborted) {
        setHistory(previous => ({ scope: owner.scope,
          items: readVersion === historyWriteVersion.current || previous.scope !== owner.scope ? result.items
            : [...previous.items, ...result.items.filter(item => !previous.items.some(saved => saved.preview_id === item.preview_id))].slice(0, 10),
          loaded: true, unavailable: false }))
        const last = unresolved.current ?? attempts.current
        if (last?.scope === owner.scope && last.actionId && result.items.some(item => item.client_action_id === last.actionId
            && item.generation === last.generation && item.phase === last.phase
            && (!item.page_draft || item.page_draft.page_token === last.pageToken))) {
          last.state = 'saved'
          if (unresolved.current === last) unresolved.current = null
          setCaptureStatus(status => status === 'unconfirmed' || status === 'interrupted' ? 'idle' : status)
        }
      }
    }).catch(() => {
      if (ownerRef.current === owner && !controller.signal.aborted) {
        setHistory(previous => ({ ...previous, unavailable: true }))
      }
    })
    resolveAttempt()
  }, [resolveAttempt, scope, sessionId])

  useLayoutEffect(() => {
    if (!scope) {
      setHistory(emptyHistory)
      setCaptureStatus('idle')
      return
    }
    const owner = { scope }
    ownerRef.current = owner
    setCaptureStatus('idle')
    refresh()
    const discard = () => {
      if (ownerRef.current !== owner) return
      ownerRef.current = null
      pageEpoch.current.reset()
      pageTokenRef.current = undefined
      activeRead.current?.abort()
      cancelFramePreview()
      attempts.current = null
      unresolved.current = null
      resolution.current?.controller.abort()
      resolution.current = null
      resolutionRetryAt.current = 0
    }
    const privacyPause = () => {
      discard()
      setHistory(emptyHistory)
      setCaptureStatus('idle')
    }
    window.addEventListener('hsc-privacy-pause', privacyPause)
    return () => {
      discard()
      window.removeEventListener('hsc-privacy-pause', privacyPause)
    }
  }, [cancelFramePreview, refresh, scope])

  const sample = useCallback(({ video, features, motion, token, generation, leaseId, cameraKey, current, available, inspection, allowClear, pageChanged }: PreviewSample) => {
    const owner = ownerRef.current
    if (!owner || owner.scope !== scope || !current()) {
      cancelFramePreview()
      return
    }
    const pageToken = pageEpoch.current.observe(cameraKey, token, Date.now(), features, pageChanged)
    if (pageTokenRef.current !== pageToken) {
      cancelFramePreview()
      attempts.current = null
      pageTokenRef.current = pageToken
    }
    const warmth = previewWarmth(features, motion, Boolean(inspection) || allowClear)
    if (!warmth) {
      cancelFramePreview()
      return
    }
    const inspectionTurnId = inspection?.turnId
    const inProgress = pending.current
    if (inProgress && (inProgress.cameraKey !== cameraKey || inProgress.inspectionTurnId !== inspectionTurnId
        || inProgress.warmth !== warmth || coarseDistance(features.fingerprint, inProgress.frame.fingerprint) > maximumMotion
        || (inspection && previewContentChanged(inProgress.frame, features)))) cancelFramePreview()
    const now = Date.now()
    if (unresolved.current) {
      resolveAttempt()
      return
    }
    if (now < busyRetryAt.current || !inspection && now < retiredRetryAt.current) return
    const last = attempts.current?.scope === scope ? attempts.current : null
    const refreshDue = last?.state === 'saved' && now - last.at >= refreshAttemptInterval
    const changedSinceAttempt = !!inspection || !last || refreshDue || previewContentChanged(last.frame, features)
    const previous = windowRef.current
    if (previous?.cameraKey === cameraKey && token === previous.token) return
    if (previous?.cameraKey === cameraKey && token < previous.token) cancelFramePreview()
    const canExtend = previous?.cameraKey === cameraKey && token > previous.token && now - previous.lastAt <= maximumGap
      && previous.warmth === warmth && previous.reference === last && previous.changedSinceAttempt === changedSinceAttempt
      && previous.inspectionTurnId === inspectionTurnId
      && coarseDistance(features.fingerprint, previous.anchor) <= maximumMotion
    const observed = { cameraKey, inspectionTurnId, warmth, changedSinceAttempt, reference: last, token,
      firstAt: canExtend ? previous.firstAt : now, lastAt: now,
      count: canExtend ? previous.count + 1 : 1, anchor: canExtend ? previous.anchor : features.fingerprint.slice() }
    windowRef.current = observed
    const highWarmth = warmth === 'high'
    if (!available || pending.current || observed.count < (highWarmth ? 7 : sampleCount)
        || now - observed.firstAt < (highWarmth ? 3_000 : minimumSpan)
        || (inspection ? last?.inspectionTurnId === inspectionTurnId
          : !history.loaded || history.scope !== scope || !!(last && now - last.at < minimumAttemptInterval) || !changedSinceAttempt)) return
    let frozen: ReturnType<typeof frozenPreviewImage>
    try { frozen = frozenPreviewImage(video, features, warmth) } catch { /* Camera frame is no longer usable. */ }
    if (!frozen || !current()) {
      cancelFramePreview()
      return
    }
    if (!inspection && last && !refreshDue && !previewContentChanged(last.frame, frozen.features)) return
    const controller = new AbortController()
    const capture: PendingPreview = { controller, cameraKey, inspectionTurnId, warmth, frame: frozen.features }
    const inScope = () => ownerRef.current === owner && pending.current === capture && pageTokenRef.current === pageToken && current()
    pending.current = capture
    void (async () => {
      try {
        const signature = await frameSignature(frozen.image)
        if (!inScope() || controller.signal.aborted) return
        if (!inspection && !last && signature && history.items.some(item => item.frame_signature === signature
            && (!item.page_draft || item.page_draft.page_token === pageToken))) {
          attempts.current = { scope: owner.scope, frame: frozen.features, at: now, state: 'saved' }
          return
        }
        const actionId = crypto.randomUUID()
        const attempt: PreviewAttempt = { scope: owner.scope, actionId, inspectionTurnId, generation, leaseId, phase, pageToken,
          frame: frozen.features, at: now, state: 'pending' }
        capture.attempt = attempts.current = attempt
        setCaptureStatus('reading')
        if (refreshDue && !inspection) previewDiagnostic('refresh_due', now)
        previewDiagnostic('started', now)
        const result = await localApi.createDocumentMonitorPreview(sessionId, frozen.image, generation, actionId, controller.signal, leaseId, signature, inspectionTurnId, phase, pageToken)
        if (!inScope() || controller.signal.aborted) return
        if (result.page_draft && result.page_draft.page_token !== pageToken) throw new Error('DOCUMENT_PREVIEW_PAGE_RESPONSE_MISMATCH')
        const inspectionText = inspection && result.inspection_response_text?.trim()
        if (inspection && (result.inspection_turn_id !== inspectionTurnId || result.client_action_id !== actionId
            || result.generation !== generation || result.phase !== phase || !result.preview_id || !inspectionText)) {
          throw new Error('DOCUMENT_PREVIEW_INSPECTION_RESPONSE_MISMATCH')
        }
        attempt.state = 'saved'
        previewDiagnostic('saved', now, result.question_count)
        historyWriteVersion.current += 1
        setHistory(previous => previous.scope === owner.scope ? ({
          ...previous, items: [result, ...previous.items.filter(item => item.preview_id !== result.preview_id)].slice(0, 10)
        }) : previous)
        setCaptureStatus('idle')
        if (inspection && inspectionText) {
          inspection.report({ preview_id: result.preview_id, page_status: 'partial_local_preview', response_text: inspectionText })
        }
      } catch (error) {
        if (capture.attempt && inScope() && !controller.signal.aborted) {
          if (error instanceof ApiError && error.status === 409
              && (error.code === 'DOCUMENT_OCR_BUSY' || error.message === 'DOCUMENT_OCR_BUSY')) {
            // This response confirms no preview was committed. Keep the named
            // inspection alive and retry from new pixels after the old OCR
            // releases its server lock; unknown outcomes must not use this path.
            attempts.current = null
            windowRef.current = null
            busyRetryAt.current = Date.now() + (inspection ? 5_000 : minimumAttemptInterval)
            setCaptureStatus(inspection ? 'reading' : 'idle')
            return
          }
          capture.attempt.state = 'unconfirmed'
          unresolved.current = capture.attempt
          previewDiagnostic('unconfirmed', capture.attempt.at, undefined, error)
          setCaptureStatus('unconfirmed')
          // The POST may have saved before the response was lost. Recover by
          // reading history instead of submitting another image.
          refresh()
          inspection?.unavailable()
        }
      } finally {
        if (pending.current === capture) {
          pending.current = null
          if (capture.attempt && controller.signal.aborted && ownerRef.current === owner) refresh()
        }
      }
    })()
  }, [cancelFramePreview, history, phase, refresh, resolveAttempt, scope, sessionId])

  return {
    visible: scope !== null && history.scope === scope,
    history: history.scope === scope ? history : emptyHistory,
    captureStatus,
    sample,
    cancelFramePreview,
    invalidatePage,
    refresh
  }
}
