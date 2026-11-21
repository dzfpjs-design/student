import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import { localApi } from './api'
import { extractDocumentFrameFeatures, type DocumentFrameFeatures } from './documentFrameMonitor'
import type { DocumentOcrPreview } from './types'

const sampleCount = 4
const minimumSpan = 1_500
const maximumGap = 1_250
const maximumMotion = 1
const minimumAttemptInterval = 20_000

type PreviewSample = {
  video: HTMLVideoElement
  features: DocumentFrameFeatures
  motion: number
  token: number
  generation: number
  cameraKey: string
  current: () => boolean
  available: boolean
}
type SampleWindow = { cameraKey: string; token: number; firstAt: number; lastAt: number; count: number; anchor: Uint8Array }
type PreviewHistory = { scope: string | null; items: DocumentOcrPreview[]; loaded: boolean; unavailable: boolean }
const emptyHistory: PreviewHistory = { scope: null, items: [], loaded: false, unavailable: false }
type CaptureStatus = 'idle' | 'reading' | 'interrupted' | 'unconfirmed'

function previewEligible(features: DocumentFrameFeatures, motion: number): boolean {
  return features.paperRatio >= .10 && features.brightness >= 28 && features.contrast >= 7
    && features.skinRatio >= .10 && features.skinRatio < .22 && motion <= maximumMotion
}

function coarseDistance(left: Uint8Array, right: Uint8Array): number {
  if (!left.length || left.length !== right.length) return Infinity
  return left.reduce((sum, value, index) => sum + Math.abs(value - right[index]), 0) / left.length
}

function frozenPreviewImage(video: HTMLVideoElement, observed: DocumentFrameFeatures): string | undefined {
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
  if (!previewEligible(features, coarseDistance(features.fingerprint, observed.fingerprint))) return
  return snapshot.toDataURL('image/jpeg', .82).split(',', 2)[1] || undefined
}

export function useDocumentMonitorPreviews(sessionId: string, childId: string | undefined, enabled: boolean) {
  const scope = enabled && childId ? JSON.stringify([sessionId, childId]) : null
  const ownerRef = useRef<{ scope: string } | null>(null)
  const windowRef = useRef<SampleWindow | null>(null)
  const activeRead = useRef<AbortController | null>(null)
  const pending = useRef<{ controller: AbortController; cameraKey: string; anchor: Uint8Array } | null>(null)
  const attempts = useRef<{ scope: string; generation: number; at: number } | null>(null)
  const [history, setHistory] = useState(emptyHistory)
  const [captureStatus, setCaptureStatus] = useState<CaptureStatus>('idle')

  const cancelFramePreview = useCallback(() => {
    windowRef.current = null
    const current = pending.current
    if (current && !current.controller.signal.aborted) {
      current.controller.abort()
      if (ownerRef.current) setCaptureStatus('interrupted')
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
        if (last?.scope === owner.scope && result.items.some(item => item.generation === last.generation)) {
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

  const sample = useCallback(({ video, features, motion, token, generation, cameraKey, current, available }: PreviewSample) => {
    const owner = ownerRef.current
    if (!owner || owner.scope !== scope || !current() || !previewEligible(features, motion)) {
      cancelFramePreview()
      return
    }
    if (pending.current && (pending.current.cameraKey !== cameraKey
        || coarseDistance(features.fingerprint, pending.current.anchor) > maximumMotion)) cancelFramePreview()
    const now = Date.now()
    const previous = windowRef.current
    if (previous?.cameraKey === cameraKey && token === previous.token) return
    if (previous?.cameraKey === cameraKey && token < previous.token) cancelFramePreview()
    const canExtend = previous?.cameraKey === cameraKey && token > previous.token && now - previous.lastAt <= maximumGap
      && coarseDistance(features.fingerprint, previous.anchor) <= maximumMotion
    const observed = { cameraKey, token, firstAt: canExtend ? previous.firstAt : now, lastAt: now,
      count: canExtend ? previous.count + 1 : 1, anchor: canExtend ? previous.anchor : features.fingerprint.slice() }
    windowRef.current = observed
    const last = attempts.current
    if (!available || pending.current || !history.loaded || history.scope !== scope
        || observed.count < sampleCount || now - observed.firstAt < minimumSpan
        || history.items.some(item => item.generation === generation)
        || (last?.scope === scope && (last.generation === generation || now - last.at < minimumAttemptInterval))) return
    let image: string | undefined
    try { image = frozenPreviewImage(video, features) } catch { /* Camera frame is no longer usable. */ }
    if (!image || !current()) {
      cancelFramePreview()
      return
    }
    const controller = new AbortController()
    const attempt = { controller, cameraKey, anchor: features.fingerprint.slice() }
    const inScope = () => ownerRef.current === owner && pending.current === attempt && current()
    pending.current = attempt
    attempts.current = { scope: owner.scope, generation, at: now }
    setCaptureStatus('reading')
    void localApi.createDocumentMonitorPreview(sessionId, image, generation, crypto.randomUUID(), controller.signal).then(result => {
      if (!inScope() || controller.signal.aborted) return
      setHistory(previous => previous.scope === owner.scope ? ({
        ...previous, items: [result, ...previous.items.filter(item => item.preview_id !== result.preview_id)]
      }) : previous)
      setCaptureStatus('idle')
    }).catch(() => {
      if (inScope() && !controller.signal.aborted) {
        setCaptureStatus('unconfirmed')
        // The POST may have saved before the response was lost. Recover by
        // reading history instead of submitting another image.
        refresh()
      }
    }).finally(() => { if (pending.current === attempt) pending.current = null })
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
