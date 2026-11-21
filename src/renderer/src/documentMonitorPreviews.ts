import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import { localApi } from './api'
import { contentChanged, extractDocumentFrameFeatures, type DocumentFrameFeatures } from './documentFrameMonitor'
import type { DocumentOcrPreview } from './types'

const sampleCount = 4
const minimumSpan = 1_500
const maximumGap = 1_250
const maximumMotion = 1
const minimumAttemptInterval = 20_000
const previewPixelChange = 12

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
}
type Warmth = 'warm' | 'high'
type PreviewAttempt = {
  scope: string
  actionId?: string
  at: number
  frame: DocumentFrameFeatures
  state: 'pending' | 'saved' | 'interrupted' | 'unconfirmed'
}
type SampleWindow = {
  cameraKey: string
  warmth: Warmth
  changedSinceAttempt: boolean
  reference: PreviewAttempt | null
  token: number
  firstAt: number
  lastAt: number
  count: number
  anchor: Uint8Array
}
type PendingPreview = { controller: AbortController; cameraKey: string; warmth: Warmth; anchor: Uint8Array; attempt?: PreviewAttempt }
type PreviewHistory = { scope: string | null; items: DocumentOcrPreview[]; loaded: boolean; unavailable: boolean }
const emptyHistory: PreviewHistory = { scope: null, items: [], loaded: false, unavailable: false }
type CaptureStatus = 'idle' | 'reading' | 'interrupted' | 'unconfirmed'

function previewWarmth(features: DocumentFrameFeatures, motion: number): Warmth | null {
  if (features.paperRatio < .10 || features.brightness < 28 || features.contrast < 7
      || features.skinRatio < .10 || motion > maximumMotion) return null
  if (features.skinRatio < .22) return 'warm'
  // Color alone cannot tell a stationary hand from a warm backdrop. Require
  // a stronger paper/contrast signal and more settled frames for a partial read.
  return features.skinRatio < .65 && features.paperRatio >= .20 && features.contrast >= 16 ? 'high' : null
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
  if (previewWarmth(features, coarseDistance(features.fingerprint, observed.fingerprint)) !== warmth) return
  const image = snapshot.toDataURL('image/jpeg', .82).split(',', 2)[1]
  return image ? { image, features } : undefined
}

export function useDocumentMonitorPreviews(sessionId: string, childId: string | undefined, enabled: boolean) {
  const scope = enabled && childId ? JSON.stringify([sessionId, childId]) : null
  const ownerRef = useRef<{ scope: string } | null>(null)
  const windowRef = useRef<SampleWindow | null>(null)
  const activeRead = useRef<AbortController | null>(null)
  const pending = useRef<PendingPreview | null>(null)
  const attempts = useRef<PreviewAttempt | null>(null)
  const [history, setHistory] = useState(emptyHistory)
  const [captureStatus, setCaptureStatus] = useState<CaptureStatus>('idle')

  const cancelFramePreview = useCallback(() => {
    windowRef.current = null
    const current = pending.current
    if (current && !current.controller.signal.aborted) {
      current.controller.abort()
      if (current.attempt) {
        current.attempt.state = 'interrupted'
        if (ownerRef.current) setCaptureStatus('interrupted')
      }
    }
  }, [])

  const refresh = useCallback(() => {
    const owner = ownerRef.current
    if (!owner || owner.scope !== scope) return
    activeRead.current?.abort()
    const controller = new AbortController()
    activeRead.current = controller
    setHistory(previous => ({ scope: owner.scope, items: previous.scope === scope ? previous.items : [], loaded: false, unavailable: false }))
    void localApi.documentMonitorPreviews(sessionId, controller.signal).then(result => {
      if (ownerRef.current === owner && !controller.signal.aborted) {
        setHistory({ scope: owner.scope, items: result.items, loaded: true, unavailable: false })
        const last = attempts.current
        if (last?.scope === owner.scope && last.actionId && result.items.some(item => item.client_action_id === last.actionId)) {
          last.state = 'saved'
          setCaptureStatus(status => status === 'unconfirmed' || status === 'interrupted' ? 'idle' : status)
        }
      }
    }).catch(() => {
      if (ownerRef.current === owner && !controller.signal.aborted) {
        setHistory(previous => ({ ...previous, unavailable: true }))
      }
    })
  }, [scope, sessionId])

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
      activeRead.current?.abort()
      cancelFramePreview()
      attempts.current = null
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

  const sample = useCallback(({ video, features, motion, token, generation, leaseId, cameraKey, current, available }: PreviewSample) => {
    const owner = ownerRef.current
    const warmth = previewWarmth(features, motion)
    if (!owner || owner.scope !== scope || !current() || !warmth) {
      cancelFramePreview()
      return
    }
    if (pending.current && (pending.current.cameraKey !== cameraKey
        || pending.current.warmth !== warmth || coarseDistance(features.fingerprint, pending.current.anchor) > maximumMotion)) cancelFramePreview()
    const now = Date.now()
    const last = attempts.current?.scope === scope ? attempts.current : null
    const changedSinceAttempt = !last || previewContentChanged(last.frame, features)
    const previous = windowRef.current
    if (previous?.cameraKey === cameraKey && token === previous.token) return
    if (previous?.cameraKey === cameraKey && token < previous.token) cancelFramePreview()
    const canExtend = previous?.cameraKey === cameraKey && token > previous.token && now - previous.lastAt <= maximumGap
      && previous.warmth === warmth && previous.reference === last && previous.changedSinceAttempt === changedSinceAttempt
      && coarseDistance(features.fingerprint, previous.anchor) <= maximumMotion
    const observed = { cameraKey, warmth, changedSinceAttempt, reference: last, token,
      firstAt: canExtend ? previous.firstAt : now, lastAt: now,
      count: canExtend ? previous.count + 1 : 1, anchor: canExtend ? previous.anchor : features.fingerprint.slice() }
    windowRef.current = observed
    const highWarmth = warmth === 'high'
    if (!available || pending.current || !history.loaded || history.scope !== scope
        || observed.count < (highWarmth ? 7 : sampleCount) || now - observed.firstAt < (highWarmth ? 3_000 : minimumSpan)
        || (last && now - last.at < minimumAttemptInterval) || !changedSinceAttempt) return
    let frozen: ReturnType<typeof frozenPreviewImage>
    try { frozen = frozenPreviewImage(video, features, warmth) } catch { /* Camera frame is no longer usable. */ }
    if (!frozen || !current()) {
      cancelFramePreview()
      return
    }
    if (last && !previewContentChanged(last.frame, frozen.features)) return
    const controller = new AbortController()
    const capture: PendingPreview = { controller, cameraKey, warmth, anchor: frozen.features.fingerprint.slice() }
    const inScope = () => ownerRef.current === owner && pending.current === capture && current()
    pending.current = capture
    void (async () => {
      try {
        const signature = await frameSignature(frozen.image)
        if (!inScope() || controller.signal.aborted) return
        if (!last && signature && history.items.some(item => item.frame_signature === signature)) {
          attempts.current = { scope: owner.scope, frame: frozen.features, at: 0, state: 'saved' }
          return
        }
        const actionId = crypto.randomUUID()
        const attempt: PreviewAttempt = { scope: owner.scope, actionId, frame: frozen.features, at: now, state: 'pending' }
        capture.attempt = attempts.current = attempt
        setCaptureStatus('reading')
        const result = await localApi.createDocumentMonitorPreview(sessionId, frozen.image, generation, actionId, controller.signal, leaseId, signature)
        if (!inScope() || controller.signal.aborted) return
        attempt.state = 'saved'
        setHistory(previous => previous.scope === owner.scope ? ({
          ...previous, items: [result, ...previous.items.filter(item => item.preview_id !== result.preview_id)].slice(0, 10)
        }) : previous)
        setCaptureStatus('idle')
      } catch {
        if (capture.attempt && inScope() && !controller.signal.aborted) {
          capture.attempt.state = 'unconfirmed'
          setCaptureStatus('unconfirmed')
          // The POST may have saved before the response was lost. Recover by
          // reading history instead of submitting another image.
          refresh()
        }
      } finally {
        if (pending.current === capture) {
          pending.current = null
          if (capture.attempt && controller.signal.aborted && ownerRef.current === owner) refresh()
        }
      }
    })()
  }, [cancelFramePreview, history, refresh, scope, sessionId])

  return {
    visible: scope !== null && history.scope === scope,
    history: history.scope === scope ? history : emptyHistory,
    captureStatus,
    sample,
    cancelFramePreview,
    refresh
  }
}
