import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { FileScan, Pause, Play, RotateCw } from 'lucide-react'
import { ApiError, localApi } from './api'
import { recoverDocumentOperations } from './documentOperationRecovery'
import { DocumentFrameMonitor, documentFrameDiagnosticBands, extractDocumentFrameFeatures, fingerprintSignature, type DocumentFrameDecision, type DocumentFrameFeatures } from './documentFrameMonitor'
import { configureDocumentCameraAutofocus, mediaDeviceFailure, mediaDeviceRegistration, openDocumentCameraStream, resolveBoundVideoDevice } from './mediaDevices'
import type { DocumentMonitorState, DocumentMonitorStatus, DocumentPageRevision } from './types'
import { CAMERA_DEVICE_COMMAND, CAMERA_DEVICE_STATUS, CameraFrameClock, type CameraDeviceCommand, type CameraStatusRequest, type DocumentInspectionOutput, requestCameraDevice, waitForCameraFrame } from './cameraDeviceRuntime'
import { documentCameraReservedForCapture, registerDocumentMonitorHandoff } from './documentCameraHandoff'
import { selectOcrTextCandidate } from './tutorOcrEvidence'
import { publishVoiceDocumentCapture, releaseVoiceDocumentCapture } from './voiceCaptureContext'
import { DocumentVisibilityChannel } from './documentVisibilityChannel'
import { useDocumentMonitorPreviews } from './documentMonitorPreviews'
import { mistakeLearningApi } from './mistakeLearningApi'
import type { RefObject } from 'react'

type RuntimeStatus = 'inactive' | 'starting' | 'active' | 'paused' | 'unavailable'
const DOCUMENT_INSPECTION_WATCHDOG_MS = 180_000
const DEVICE_LEASE_RELEASE_TIMEOUT_MS = 5_000
type MonitorDiagnostic = 'frame_state' | 'frame_stalled' | 'ocr_started' | 'ocr_pending' | 'ocr_result' | 'ocr_discarded' | 'ocr_retry_considered' | 'revision_saved' | 'revision_inflight_interrupted' | 'revision_historical' | 'ocr_failed' | 'ocr_unconfirmed' | 'revision_failed' | 'revision_unconfirmed' | 'post_revision_failed'
function monitorDiagnostic(event: MonitorDiagnostic, details: Record<string, unknown> = {}, level: 'info' | 'warn' = 'info'): void {
  try { void window.desktop?.recordRendererDiagnostic?.({ event: `document.monitor.${event}`, level, details })?.catch(() => undefined) }
  catch { /* Diagnostics must never interrupt the camera or OCR. */ }
}
type CloudPageFeedback = { blocked: boolean; message: string; text: string; pageInkUploaded: boolean }

function cloudPageFeedback(value: unknown): CloudPageFeedback | null {
  if (!value || typeof value !== 'object') return null
  const result = value as { status?: string; state?: string; error_code?: string; reason?: string; blocks?: Array<{ text?: unknown }>; privacy?: { uploaded?: string } }
  const status = result.status ?? result.state
  if (status === 'needs_review' || status === 'needs_confirmation') {
    const text = Array.isArray(result.blocks)
      ? result.blocks.map((block) => typeof block?.text === 'string' ? block.text.trim() : '').filter(Boolean).join('\n')
      : ''
    return {
      blocked: false,
      message: text ? '云端已给出待核对文字，尚未确认为题干或错题事实。' : '云端已返回，但没有可核对的文字；请调整页面后再检查。',
      text,
      pageInkUploaded: result.privacy?.uploaded === 'page_region_binary_ink',
    }
  }
  if (status !== 'blocked') return null
  const reason = result.error_code ?? result.reason
  const messages: Record<string, string> = {
    DOCUMENT_VISION_NOT_CONFIGURED: '尚未配置云端页面解析，请家长完成配置；本地识别文字仍需核对。',
    DOCUMENT_VISION_CLOUD_DISABLED: '云端页面解析已关闭，本次只保留本地识别结果。',
    DOCUMENT_VISION_CLOUD_NOT_ALLOWED: '云端页面解析未获授权，本次只保留本地识别结果。',
    DOCUMENT_VISION_BUDGET_EXCEEDED: '云端页面解析预算不足，暂未获得新的待核对文字。',
    DOCUMENT_VISION_FACE_DETECTED: '画面包含人脸，不满足安全解析条件，未发送本次页内图。',
    DOCUMENT_VISION_PII_DETECTED: '页面可能含个人信息，不满足安全解析条件，未发送本次页内图。',
    DOCUMENT_VISION_PAGE_BOUNDARY_UNPROVEN: '无法确认安全的页面边界，未发送本次页内图；请摆正书本或作业纸。',
    DOCUMENT_VISION_LOCAL_REGIONS_UNPROVEN: '无法确认安全的页内文字范围，未发送本次页内图；请调整页面。',
    DOCUMENT_VISION_NON_DOCUMENT_CONTENT: '画面未通过纯文档安全检查，未发送本次页内图。',
    DOCUMENT_VISION_PRIVACY_CHECK_UNAVAILABLE: '本地安全检查暂不可用，未发送本次页内图；请家长检查配置。',
    DOCUMENT_VISION_RATE_LIMITED: '云端页面解析正在限流，稍后才能重试。',
    DOCUMENT_VISION_COOLDOWN: '云端页面解析正在冷却等待，稍后才能重试。',
    DOCUMENT_VISION_BUSY: '云端页面解析正忙，稍后才能重试。',
    DOCUMENT_VISION_TIMEOUT: '云端页面解析响应超时，尚未取得可核对结果。',
    DOCUMENT_VISION_NETWORK_ERROR: '云端页面解析连接失败，尚未取得可核对结果。',
    DOCUMENT_VISION_INVALID_RESPONSE: '云端返回的内容未通过校验，不能作为题干或错题事实。',
  }
  return { blocked: true, message: messages[reason ?? ''] ?? '页面云端解析暂不可用，已有本地识别文字仍需核对。', text: '', pageInkUploaded: false }
}

export function ContinuousDocumentMonitor({
  sessionId,
  childId,
  active,
  analysisActive = true,
  allowDiagnosticFrameCapture = true,
  allowLocalOcrPreview = false,
  onReviewRevision, onSavedRevision, onStatus, reviewCapture
}: {
  sessionId: string
  childId?: string
  active: boolean
  analysisActive?: boolean
  allowDiagnosticFrameCapture?: boolean
  allowLocalOcrPreview?: boolean
  onReviewRevision?: (revision: DocumentPageRevision) => 'mistake' | 'tutor' | void
  onSavedRevision?: (revision: DocumentPageRevision) => void
  onStatus?: (status: RuntimeStatus) => void
  reviewCapture?: RefObject<((signal: AbortSignal) => Promise<string>) | null>
}): React.JSX.Element {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const firstFrameTimerRef = useRef<number | null>(null)
  const firstFramePollRef = useRef<number | null>(null)
  const firstVideoFrameRef = useRef<number | null>(null)
  const leaseRef = useRef<string | null>(null)
  const trialPolicyRef = useRef<DocumentMonitorStatus['trial_capture_policy']>(undefined)
  const monitorGenerationRef = useRef<number | undefined>(undefined)
  const nextTrialCaptureAtRef = useRef(0)
  const trialCaptureBusyRef = useRef(false)
  const generationRef = useRef(0)
  const documentCaptureRef = useRef<{ page: DocumentPageRevision | null; owner?: symbol }>({ page: null })
  const openingRef = useRef<{ generation: number; reopen: boolean; settled: Promise<void> } | null>(null)
  const lastOpenIncident = useRef<string | null>(null)
  const runningRef = useRef(false)
  const analysisActiveRef = useRef(analysisActive)
  const allowDiagnosticFrameCaptureRef = useRef(allowDiagnosticFrameCapture)
  allowDiagnosticFrameCaptureRef.current = allowDiagnosticFrameCapture
  const openCameraRef = useRef<() => Promise<void>>(async () => undefined)
  const reviewRef = useRef(onReviewRevision)
  const savedRevisionRef = useRef(onSavedRevision)
  savedRevisionRef.current = onSavedRevision
  const monitorRef = useRef(new DocumentFrameMonitor())
  const processingRef = useRef(false)
  const freshnessEpochRef = useRef(0)
  const lifecycleEpochRef = useRef(0)
  const visualInterruptionEpochRef = useRef(0)
  const freshnessInvalidRef = useRef(false)
  const lastEventAtRef = useRef(Number.NEGATIVE_INFINITY)
  const lastDiagnosticAtRef = useRef(Number.NEGATIVE_INFINITY)
  const lastStallDiagnosticAtRef = useRef(Number.NEGATIVE_INFINITY)
  const lastFramingGuidanceRef = useRef({ state: '' as DocumentMonitorState | '', at: Number.NEGATIVE_INFINITY })
  const forceCaptureRef = useRef(false)
  const unconfirmedCaptureRef = useRef(false)
  const pendingInspectRef = useRef<(((success: boolean, output?: DocumentInspectionOutput) => void) & { deferReview?: boolean }) | null>(null)
  const restoredSessionRef = useRef<string | null>(null)
  const monitorStartPendingRef = useRef<Promise<Awaited<ReturnType<typeof localApi.startDocumentMonitor>>> | null>(null)
  const monitorChoicePendingRef = useRef<Promise<unknown>>(Promise.resolve())
  const deviceFrameClock = useRef(new CameraFrameClock())
  const visibilityRef = useRef<DocumentVisibilityChannel | null>(null)
  const releasePendingRef = useRef<Promise<boolean>>(Promise.resolve(true))
  const failedLeasesRef = useRef<string[]>([])
  const [pausedSession, setPausedSession] = useState<string | null>(null)
  const manualPaused = pausedSession === sessionId
  const [status, setStatus] = useState<RuntimeStatus>('inactive')
  const [frameState, setFrameState] = useState<DocumentMonitorState>('stabilizing')
  const [message, setMessage] = useState('进入作业或学习阶段后，高拍仪会自动进行本地页面识别。')
  const [revision, setRevision] = useState<DocumentPageRevision | null>(null)
  const [processing, setProcessing] = useState(false)
  const [analysisBlocked, setAnalysisBlocked] = useState(false)
  const [cloudFeedback, setCloudFeedback] = useState<CloudPageFeedback | null>(null)
  const [reconnectAttempt, setReconnectAttempt] = useState(0)
  const [recoveryNotice, setRecoveryNotice] = useState<string | null>(null)
  const [diagnosticNotice, setDiagnosticNotice] = useState<string | null>(null)
  const [deviceLabel, setDeviceLabel] = useState('尚未识别实际设备')
  const [frameDimensions, setFrameDimensions] = useState<string | null>(null)
  const [previewOrientation, setPreviewOrientation] = useState<0 | 90 | 180 | 270>(0)
  const reviewScopeRef = useRef({ sessionId, childId })
  useLayoutEffect(() => { reviewScopeRef.current = { sessionId, childId } }, [sessionId, childId])
  const shouldRun = active && !manualPaused
  const { visible: localPreviewVisible, history: localPreviewHistory, captureStatus: localPreviewStatus,
    sample: observePreview, cancelFramePreview, refresh: refreshLocalPreviews } =
    useDocumentMonitorPreviews(sessionId, childId, allowLocalOcrPreview && shouldRun && analysisActive)
  const releaseDocumentCapture = useCallback((): void => {
    const own = documentCaptureRef.current
    if (own.owner && childId) releaseVoiceDocumentCapture(sessionId, childId, own.owner)
    own.owner = undefined
  }, [childId, sessionId])
  useLayoutEffect(() => {
    analysisActiveRef.current = analysisActive
    if (!analysisActive) {
      lifecycleEpochRef.current += 1
      freshnessEpochRef.current += 1
      freshnessInvalidRef.current = true
      monitorRef.current.invalidateStability()
      visibilityRef.current?.discard()
      releaseDocumentCapture(); documentCaptureRef.current.page = null
      forceCaptureRef.current = false
      const pending = pendingInspectRef.current
      pendingInspectRef.current = null
      pending?.(false)
    }
  }, [analysisActive, releaseDocumentCapture])
  useLayoutEffect(() => { reviewRef.current = onReviewRevision }, [onReviewRevision])

  const retainTrialFrame = useCallback(async (imageBase64: string, reason: 'periodic' | 'page_changed' | 'ocr_capture'): Promise<string | undefined> => {
    if (!allowDiagnosticFrameCaptureRef.current) return undefined
    const lease = leaseRef.current, generation = monitorGenerationRef.current
    if (!trialPolicyRef.current?.enabled || !lease || generation === undefined || !runningRef.current || !analysisActiveRef.current) return undefined
    const localGeneration = generationRef.current
    try {
      const frame = await localApi.retainTrialDocumentFrame(sessionId, imageBase64, lease, generation, reason)
      if (!frame.image_available) throw new Error('TRIAL_CAPTURE_IMAGE_UNAVAILABLE')
      if (generationRef.current === localGeneration) setDiagnosticNotice('试用诊断图片已在本机保存。')
      return frame.artifact_id
    } catch {
      if (generationRef.current === localGeneration) setDiagnosticNotice('试用诊断图片未取得保存回执，请家长检查存储和诊断记录；学习识别仍会继续。')
      return undefined
    }
  }, [sessionId])

  const readLiveFeatures = useCallback((): DocumentFrameFeatures | null => {
    const video = videoRef.current
    try {
      if (!video || !deviceFrameClock.current.read('document_camera', video, streamRef.current, true, null).frame_confirmed) return null
      const canvas = document.createElement('canvas')
      canvas.width = 320
      canvas.height = Math.max(180, Math.round(video.videoHeight / video.videoWidth * canvas.width))
      const context = canvas.getContext('2d', { willReadFrequently: true })
      if (!context) return null
      context.drawImage(video, 0, 0, canvas.width, canvas.height)
      return extractDocumentFrameFeatures(context.getImageData(0, 0, canvas.width, canvas.height))
    } catch { return null }
  }, [])

  useEffect(() => { onStatus?.(status) }, [onStatus, status])
  useLayoutEffect(() => {
    if (!reviewCapture || !childId) return
    const capture = async (signal: AbortSignal): Promise<string> => {
      signal.throwIfAborted()
      const video = videoRef.current, stream = streamRef.current, lease = leaseRef.current
      if (!video || !stream || !lease || !runningRef.current || !analysisActiveRef.current || status !== 'active' || !active) {
        throw new Error('高拍仪还没有可用画面。请检查连接，或改用口述。')
      }
      if (frameState !== 'stable' || !deviceFrameClock.current.read('document_camera', video, stream, true, null).frame_confirmed) {
        throw new Error('请将写好的草稿纸放稳、移开手，等高拍仪画面稳定后再检查。')
      }
      const captureFrameToken = deviceFrameClock.current.frameToken()
      const generation = generationRef.current
      // Freeze one source frame first. Quality assessment and OCR crop must
      // come from those same pixels even if the camera advances meanwhile.
      const snapshot = document.createElement('canvas')
      snapshot.width = video.videoWidth; snapshot.height = video.videoHeight
      const snapshotContext = snapshot.getContext('2d')
      if (!snapshotContext || snapshot.width < 320 || snapshot.height <= 0) throw new Error('暂时无法读取草稿画面，请重试。')
      snapshotContext.drawImage(video, 0, 0, snapshot.width, snapshot.height)
      const sample = document.createElement('canvas')
      sample.width = 320; sample.height = Math.max(180, Math.round(snapshot.height / snapshot.width * sample.width))
      const sampleContext = sample.getContext('2d', { willReadFrequently: true })
      if (!sampleContext) throw new Error('暂时无法读取草稿画面，请重试。')
      sampleContext.drawImage(snapshot, 0, 0, sample.width, sample.height)
      const features = extractDocumentFrameFeatures(sampleContext.getImageData(0, 0, sample.width, sample.height))
      if (!monitorRef.current.matchesStableFrame(features, Date.now(), captureFrameToken)) {
        throw new Error('请将写好的草稿纸放稳、移开手，等高拍仪画面稳定后再检查。')
      }
      const canvas = document.createElement('canvas')
      // Keep the entire image: a central crop can silently remove the answer.
      // Bound oversized sources to the local OCR pixel budget without cropping.
      const scale = Math.min(1, Math.sqrt((4160 * 3120) / (snapshot.width * snapshot.height)))
      canvas.width = Math.floor(snapshot.width * scale); canvas.height = Math.floor(snapshot.height * scale)
      const context = canvas.getContext('2d')
      if (!context) throw new Error('暂时无法读取草稿画面，请重试。')
      context.drawImage(snapshot, 0, 0, canvas.width, canvas.height)
      let image = canvas.toDataURL('image/jpeg', 0.94).split(',', 2)[1]
      if (image && image.length > 24_000_000) image = canvas.toDataURL('image/jpeg', 0.8).split(',', 2)[1]
      if (!image) throw new Error('没有拍到草稿，请重试。')
      if (image.length > 24_000_000) throw new Error('草稿画面过大，暂时无法识别，请调整高拍仪分辨率后重试。')
      const result = await mistakeLearningApi.captureReview(sessionId, childId, lease, image, signal)
      signal.throwIfAborted()
      if (!runningRef.current || !analysisActiveRef.current || generation !== generationRef.current
        || leaseRef.current !== lease || streamRef.current !== stream
        || reviewScopeRef.current.sessionId !== sessionId || reviewScopeRef.current.childId !== childId
        || !/^[A-Za-z0-9_-]{1,160}$/.test(result?.artifact_id ?? '')) {
        throw new Error('高拍仪连接或识别状态有变化，请重新拍草稿。')
      }
      return result.artifact_id
    }
    reviewCapture.current = capture
    return () => { if (reviewCapture.current === capture) reviewCapture.current = null }
  }, [active, childId, frameState, readLiveFeatures, reviewCapture, sessionId, status])

  const releaseLeases = useCallback((leaseIds: string[], reason: string): Promise<boolean> => {
    const previous = releasePendingRef.current
    releasePendingRef.current = previous.then(async () => {
      // A prior release can fail while queued; retain it until a real receipt arrives.
      const leases = [...new Set([...failedLeasesRef.current, ...leaseIds])]
      failedLeasesRef.current = []
      const results = await Promise.all(leases.map(async (leaseId) => {
        const controller = new AbortController()
        let timer: number | undefined
        try {
          const saved = await new Promise<Awaited<ReturnType<typeof localApi.releaseDeviceLease>>>((resolve, reject) => {
            timer = window.setTimeout(() => {
              controller.abort()
              reject(new Error('LEASE_RELEASE_TIMEOUT'))
            }, DEVICE_LEASE_RELEASE_TIMEOUT_MS)
            // The deadline also retires transports that fail to settle on abort.
            void localApi.releaseDeviceLease(leaseId, reason, controller.signal).then(resolve, reject)
          })
          if (saved.lease_id !== leaseId || saved.status !== 'released') throw new Error('LEASE_NOT_RELEASED')
          return true
        }
        catch { failedLeasesRef.current.push(leaseId); return false }
        finally { if (timer !== undefined) window.clearTimeout(timer) }
      }))
      return results.every(Boolean) && failedLeasesRef.current.length === 0
    })
    return releasePendingRef.current
  }, [])

  const release = useCallback((reason: string): Promise<boolean> => {
    cancelFramePreview()
    lifecycleEpochRef.current += 1
    freshnessEpochRef.current += 1
    freshnessInvalidRef.current = true
    visibilityRef.current?.stop()
    visibilityRef.current = null
    releaseDocumentCapture()
    documentCaptureRef.current.page = null
    generationRef.current += 1
    if (firstFrameTimerRef.current !== null) {
      window.clearTimeout(firstFrameTimerRef.current)
      firstFrameTimerRef.current = null
    }
    if (firstFramePollRef.current !== null) {
      window.clearInterval(firstFramePollRef.current)
      firstFramePollRef.current = null
    }
    const video = videoRef.current
    if (firstVideoFrameRef.current !== null && video?.cancelVideoFrameCallback) {
      video.cancelVideoFrameCallback(firstVideoFrameRef.current)
      firstVideoFrameRef.current = null
    }
    const stream = streamRef.current
    streamRef.current = null
    stream?.getTracks().forEach((track) => track.stop())
    const leases = leaseRef.current ? [leaseRef.current] : []
    leaseRef.current = null
    trialPolicyRef.current = undefined
    monitorGenerationRef.current = undefined
    nextTrialCaptureAtRef.current = 0
    if (leases.length || failedLeasesRef.current.length) void releaseLeases(leases, reason)
    if (videoRef.current) videoRef.current.srcObject = null
    // In-flight OCR still owns backpressure until it settles, even after yield.
    forceCaptureRef.current = false
    setProcessing(false)
    monitorRef.current.reset()
    pendingInspectRef.current?.(false)
    pendingInspectRef.current = null
    return releasePendingRef.current
  }, [cancelFramePreview, releaseDocumentCapture, releaseLeases])

  const persistEvent = useCallback((decision: DocumentFrameDecision, features: DocumentFrameFeatures): void => {
    const now = Date.now()
    if ((decision.changed && now - lastDiagnosticAtRef.current >= 5_000) || now - lastDiagnosticAtRef.current >= 60_000) {
      lastDiagnosticAtRef.current = now
      monitorDiagnostic('frame_state', { frame_state: decision.state, ...documentFrameDiagnosticBands(decision, features) })
    }
    if (!decision.changed && now - lastEventAtRef.current < 60_000) return
    lastEventAtRef.current = now
    void localApi.recordDocumentMonitorEvent(sessionId, decision.state, decision.confidence, {
      motion_score: decision.motionScore,
      skin_ratio: Math.round(features.skinRatio * 1000) / 1000,
      paper_ratio: Math.round(features.paperRatio * 1000) / 1000,
      brightness: features.brightness,
      contrast: features.contrast,
      raw_frame_stored: false
    }).catch(() => undefined)
  }, [sessionId])

  const captureStablePage = useCallback(async (): Promise<void> => {
    const video = videoRef.current
    const stream = streamRef.current, lease = leaseRef.current
    if (!runningRef.current || !stream || !lease || !video || video.videoWidth < 320 || processingRef.current || unconfirmedCaptureRef.current || !monitorRef.current.canCaptureAt(Date.now())) return
    const generation = generationRef.current
    const freshnessEpoch = freshnessEpochRef.current
    const lifecycleEpoch = lifecycleEpochRef.current
    const visualEpoch = visualInterruptionEpochRef.current
    const live = (): boolean => runningRef.current && analysisActiveRef.current && generationRef.current === generation
    releaseDocumentCapture()
    documentCaptureRef.current.page = null
    processingRef.current = true
    setProcessing(true)
    const explicitlyRequested = forceCaptureRef.current
    const inspectionOwner = explicitlyRequested ? pendingInspectRef.current : null
    const current = (): boolean => live() && freshnessEpochRef.current === freshnessEpoch
      && streamRef.current === stream && leaseRef.current === lease
      && reviewScopeRef.current.sessionId === sessionId && reviewScopeRef.current.childId === childId
      && (!inspectionOwner || pendingInspectRef.current === inspectionOwner)
    let retryCurrentPage = false
    let ocrStarted = false
    let revisionStarted = false
    let revisionCompleted = false
    let ocrStartedAt = 0
    let ocrPendingTimer: number | null = null
    let discardLogged = false
    let inspectionOutput: DocumentInspectionOutput | undefined
    const scheduleInterruptedRetry = (): void => {
      if (ocrStarted && !revisionStarted && live() && lifecycleEpochRef.current === lifecycleEpoch
        && visualInterruptionEpochRef.current !== visualEpoch
        && streamRef.current === stream && leaseRef.current === lease
        && reviewScopeRef.current.sessionId === sessionId && reviewScopeRef.current.childId === childId
        && (!inspectionOwner || pendingInspectRef.current === inspectionOwner)) {
        monitorRef.current.markStaleCaptureAttempt(Date.now())
        monitorDiagnostic('ocr_retry_considered', { latency_ms: Math.max(0, Math.min(300_000, Date.now() - ocrStartedAt)) })
      }
    }
    const currentOrRetry = (): boolean => {
      if (current()) return true
      if (ocrStarted && !discardLogged) {
        discardLogged = true
        monitorDiagnostic(revisionStarted ? revisionCompleted ? 'revision_historical' : 'revision_inflight_interrupted' : 'ocr_discarded',
          { latency_ms: Math.max(0, Math.min(300_000, Date.now() - ocrStartedAt)) })
      }
      scheduleInterruptedRetry()
      if (live() && inspectionOwner && pendingInspectRef.current === inspectionOwner
        && freshnessEpochRef.current !== freshnessEpoch) {
        retryCurrentPage = true
        forceCaptureRef.current = true
        setMessage('页面已经变化，等画面稳定后重新检查。')
      }
      return false
    }
    forceCaptureRef.current = false
    setMessage('页面已经稳定，正在识别本次变化。')
    try {
      if (!deviceFrameClock.current.read('document_camera', video, stream, true, null).frame_confirmed
        || (deviceFrameClock.current.frameAgeMs() ?? Infinity) >= 1500) return
      const captureFrameToken = deviceFrameClock.current.frameToken()
      const canvas = document.createElement('canvas')
      canvas.width = video.videoWidth
      canvas.height = video.videoHeight
      const context = canvas.getContext('2d')
      if (!context) throw new Error('CANVAS_UNAVAILABLE')
      context.drawImage(video, 0, 0, canvas.width, canvas.height)
      const sample = document.createElement('canvas')
      sample.width = 320; sample.height = Math.max(180, Math.round(canvas.height / canvas.width * sample.width))
      const sampleContext = sample.getContext('2d', { willReadFrequently: true })
      if (!sampleContext) throw new Error('CANVAS_UNAVAILABLE')
      sampleContext.drawImage(canvas, 0, 0, sample.width, sample.height)
      const snapshotFeatures = extractDocumentFrameFeatures(sampleContext.getImageData(0, 0, sample.width, sample.height))
      if (!monitorRef.current.matchesStableFrame(snapshotFeatures, Date.now(), captureFrameToken, true)) {
        setMessage('页面正在变化，等画面稳定后再识别。')
        return
      }
      monitorRef.current.markCaptureAttempt(Date.now())
      const imageBase64 = canvas.toDataURL('image/jpeg', 0.94).split(',', 2)[1]
      const trialFrameId = await retainTrialFrame(imageBase64, 'ocr_capture')
      if (!currentOrRetry()) return
      ocrStarted = true
      ocrStartedAt = Date.now()
      monitorDiagnostic('ocr_started')
      ocrPendingTimer = window.setTimeout(() => {
        if (live() && lifecycleEpochRef.current === lifecycleEpoch && streamRef.current === stream && leaseRef.current === lease
          && reviewScopeRef.current.sessionId === sessionId && reviewScopeRef.current.childId === childId)
          monitorDiagnostic('ocr_pending', { latency_ms: Math.max(0, Math.min(300_000, Date.now() - ocrStartedAt)) }, 'warn')
      }, 30_000)
      const artifact = trialFrameId ? await localApi.runDocumentMonitorOcr(sessionId, imageBase64, trialFrameId)
        : await localApi.runDocumentMonitorOcr(sessionId, imageBase64)
      if (ocrPendingTimer !== null) { window.clearTimeout(ocrPendingTimer); ocrPendingTimer = null }
      if (!currentOrRetry()) return
      monitorDiagnostic('ocr_result', { latency_ms: Math.max(0, Math.min(300_000, Date.now() - ocrStartedAt)), count: artifact.extracted_text.trim() ? 1 : 0 })
      const currentFeatures = readLiveFeatures()
      const resultFrameToken = deviceFrameClock.current.frameToken()
      const resultFrameAge = deviceFrameClock.current.frameAgeMs() ?? Infinity
      if (!currentFeatures || resultFrameToken === null || captureFrameToken === null || resultFrameToken < captureFrameToken
        || (resultFrameToken === captureFrameToken && resultFrameAge > 250) || resultFrameAge >= 750
        || !monitorRef.current.matchesStableFrame(currentFeatures, Date.now(), resultFrameToken, true)) {
        if (inspectionOwner && pendingInspectRef.current === inspectionOwner) {
          retryCurrentPage = true
          forceCaptureRef.current = true
        }
        scheduleInterruptedRetry()
        if (!discardLogged) { discardLogged = true; monitorDiagnostic('ocr_discarded', { latency_ms: Math.max(0, Math.min(300_000, Date.now() - ocrStartedAt)) }) }
        setMessage('画面更新暂未确认，画面稳定后会重新识别。')
        return
      }
      const feedback = cloudPageFeedback((artifact.details as { cloud_vision_fallback?: unknown } | undefined)?.cloud_vision_fallback)
      const textCandidate = selectOcrTextCandidate(artifact)
      revisionStarted = true
      const saved = await localApi.recordDocumentMonitorRevision(
        sessionId, artifact.artifact_id, fingerprintSignature(snapshotFeatures.fingerprint)
      )
      revisionCompleted = true
      monitorDiagnostic('revision_saved', { latency_ms: Math.max(0, Math.min(300_000, Date.now() - ocrStartedAt)) })
      if (!currentOrRetry()) return
      setCloudFeedback(feedback)
      setAnalysisBlocked(feedback?.blocked ?? false)
      monitorRef.current.markCaptured(snapshotFeatures.fingerprint, Date.now(), snapshotFeatures.contentFingerprint)
      setRevision(saved)
      savedRevisionRef.current?.(saved)
      const now = readLiveFeatures()
      const afterSaveToken = deviceFrameClock.current.frameToken()
      const afterSaveAge = deviceFrameClock.current.frameAgeMs() ?? Infinity
      if (!now || afterSaveToken === null || captureFrameToken === null || afterSaveToken < captureFrameToken
        || (afterSaveToken === captureFrameToken && afterSaveAge > 250) || afterSaveAge >= 750
        || !monitorRef.current.matchesCapturedPage(now)) {
        monitorDiagnostic('revision_historical', { latency_ms: Math.max(0, Math.min(300_000, Date.now() - ocrStartedAt)) })
        // Preserve the captured page as history, never present it as the live page.
        retryCurrentPage = true
        if (explicitlyRequested) forceCaptureRef.current = true
        setMessage('刚才拍摄的页面已记录，当前页面有变化；我会等页面稳定后重新检查。')
        return
      }
      if (childId) {
        documentCaptureRef.current = { page: saved, owner: publishVoiceDocumentCapture(sessionId, childId, saved) }
      }
      const orientationQuality = artifact.details?.local_pipeline?.quality
      if (orientationQuality?.orientation_evidence?.ambiguous === false) {
        setPreviewOrientation(normalizePageOrientation(orientationQuality?.transforms?.orientation_degrees))
      }
      const preview = textCandidate.text.replace(/\s+/g, ' ').trim().slice(0, 90)
      inspectionOutput = { artifact_id: artifact.artifact_id, revision_id: saved.revision_id,
        page_status: feedback?.blocked ? 'blocked' : preview ? 'text_available' : 'empty',
        response_text: feedback?.blocked ? '页面已取得，但解析被安全或配置检查阻止，不能判断作业内容。'
          : preview ? `${textCandidate.source === 'cloud_page' ? '本地识别不够可靠，云端页内文字候选是' : '这次识别到'}：“${preview}”。文字还需核对，不代表已确认做题情况。` : '这次已取得页面，但没有识别到可靠文字，请调整页面或光线。' }
      if (saved.intervention?.message && !inspectionOwner?.deferReview
          && !(explicitlyRequested && (saved.marked_question_count ?? 0) > 0 && reviewRef.current)) {
        window.dispatchEvent(new CustomEvent('hsc-agent-intervention', {
          detail: {
            message: saved.intervention.message,
            interventionId: saved.intervention.intervention_id,
            sourceType: 'document_monitor',
            sessionId,
          }
        }))
      }
      if (!current()) return
      if (explicitlyRequested && !inspectionOwner?.deferReview && artifact.extracted_text.trim() && reviewRef.current) {
        const reviewKind = reviewRef.current(saved)
        if (!current()) return
        const preview = artifact.extracted_text.replace(/\s+/g, ' ').trim().slice(0, 90)
        if (reviewKind !== 'mistake') window.dispatchEvent(new CustomEvent('hsc-agent-intervention', {
          detail: { sessionId, sourceType: 'document_monitor', message: `识别结果是：“${preview}”。请说“题目正确”，或者直接说需要改哪几个字。` }
        }))
      }
      setMessage(feedback
        ? feedback.blocked ? '本地页面已记录，云端解析受阻。' : '页面解析已返回，文字尚待核对。'
        : artifact.extracted_text.trim()
        ? `已在本机记录第${saved.revision_index}个页面版本。${saved.review_required ? '手写或公式需要核对，不会自动判错。' : saved.study_signal?.kind === 'progress_observed' ? '识别到的页面文字与上次不同。' : '页面文字已记录；仅凭两次识别无法判断期间是否持续停留。'}`
        : '页面已检查，但没有识别到可靠文字；请调整位置或光线。')
    } catch (cause) {
      const unconfirmed = cause instanceof ApiError && cause.code === 'DOCUMENT_OPERATION_RESULT_UNCONFIRMED'
      if (ocrStarted) monitorDiagnostic(revisionStarted ? revisionCompleted ? 'post_revision_failed' : unconfirmed ? 'revision_unconfirmed' : 'revision_failed' : unconfirmed ? 'ocr_unconfirmed' : 'ocr_failed',
        { latency_ms: Math.max(0, Math.min(300_000, Date.now() - ocrStartedAt)) }, 'warn')
      if (live() && cause instanceof ApiError && cause.code === 'DOCUMENT_OPERATION_RESULT_UNCONFIRMED') {
        unconfirmedCaptureRef.current = true
        forceCaptureRef.current = false
        const pending = pendingInspectRef.current
        pendingInspectRef.current = null
        pending?.(false)
        setAnalysisBlocked(true)
        setMessage('这次页面操作没有确认保存结果，不会自动重发；需要时请再说“检查当前页”。')
        return
      }
      if (!current()) return
      if (cause instanceof ApiError && cause.status === 409 && (cause.code === 'DOCUMENT_OCR_BUSY' || cause.message === 'DOCUMENT_OCR_BUSY')) {
        setMessage('页面解析正忙，本次检查没有完成；你可以继续说话。')
        return
      }
      const error = cause instanceof ApiError ? cause.message : '页面识别失败。'
      setMessage(`${error} 监控仍在继续，稍后会重试识别。`)
      void localApi.recordDocumentMonitorEvent(sessionId, 'capture_failed', 1, {
        capture_reason: cause instanceof ApiError ? cause.code ?? 'LOCAL_OCR_FAILED' : 'LOCAL_OCR_FAILED',
        raw_frame_stored: false
      }).catch(() => undefined)
    } finally {
      if (ocrPendingTimer !== null) window.clearTimeout(ocrPendingTimer)
      // A released OCR may settle after another inspection was accepted.
      // Only the request that owned this frame can consume its receipt.
      if (!retryCurrentPage && inspectionOwner && pendingInspectRef.current === inspectionOwner) {
        const succeeded = current() && Boolean(inspectionOutput)
        pendingInspectRef.current = null
        inspectionOwner(succeeded, succeeded ? inspectionOutput : undefined)
      }
      processingRef.current = false
      if (live()) setProcessing(false)
    }
  }, [childId, readLiveFeatures, releaseDocumentCapture, retainTrialFrame, sessionId])

  const saveMonitorChoice = useCallback((choice: 'pause' | 'resume') => {
    const starting = monitorStartPendingRef.current
    const saving = monitorChoicePendingRef.current.then(async () => {
      await starting?.catch(() => undefined)
      if (choice === 'pause') return localApi.pauseDocumentMonitor(sessionId)
      try {
        return await localApi.resumeDocumentMonitor(sessionId)
      } catch (error) {
        // An explicit pause can stop the camera before the initial monitor row exists.
        const previous = await localApi.documentMonitorStatus(sessionId)
        if (previous.status === 'not_started') return previous
        throw error
      }
    })
    monitorChoicePendingRef.current = saving.catch(() => undefined)
    return saving
  }, [sessionId])

  const openCamera = useCallback(async (): Promise<void> => {
    if (!runningRef.current || streamRef.current || documentCameraReservedForCapture()) return
    if (openingRef.current) {
      if (openingRef.current.generation !== generationRef.current) openingRef.current.reopen = true
      return
    }
    setStatus('starting')
    setMessage('正在连接已绑定的高拍仪。')
    const generation = ++generationRef.current
    let settleOpening!: () => void
    const settled = new Promise<void>(resolve => { settleOpening = resolve })
    const opening = { generation, reopen: false, settled }
    openingRef.current = opening
    const current = (): boolean => runningRef.current && generationRef.current === generation
    let bindingFailure: { code: string; message: string } | undefined
    try {
      let released = await releasePendingRef.current
      if (!current()) return
      if (!released || failedLeasesRef.current.length) released = await releaseLeases([], 'document_monitor_reconnect')
      if (!current()) return
      if (!released) {
        setStatus('unavailable')
        setReconnectAttempt((value) => value + 1)
        setMessage('高拍仪还在关闭，画面读取已停止；关闭确认后会自动重新连接。')
        return
      }
      if (restoredSessionRef.current !== sessionId) {
        const previous = await localApi.documentMonitorStatus(sessionId)
        if (!current()) return
        restoredSessionRef.current = sessionId
        if (previous.status === 'paused') {
          runningRef.current = false
          setPausedSession(sessionId)
          setStatus('paused')
          setMessage('高拍仪按上次选择保持暂停。')
          return
        }
      }
      try {
        const recovered = await recoverDocumentOperations(sessionId, localApi.pendingDocumentReceipts, localApi.acknowledgeDocumentReceipt, current)
        if (!current()) return
        unconfirmedCaptureRef.current = recovered.unconfirmed > 0
        setAnalysisBlocked(recovered.unconfirmed > 0)
        const notice = recovered.unconfirmed ? '上次页面操作还有未确认的结果，我不会自动重拍；需要时请说“检查当前页”。'
          : recovered.saved ? '上次页面操作的保存结果已找回，没有重新识别图片；这还不代表题目已核对或掌握。' : null
        setRecoveryNotice(notice)
        if (notice) window.dispatchEvent(new CustomEvent('hsc-agent-intervention', { detail: { sessionId, sourceType: 'document_monitor', message: notice } }))
      } catch {
        if (!current()) return
        unconfirmedCaptureRef.current = true
        setAnalysisBlocked(true)
        setRecoveryNotice('上次页面操作的恢复状态暂时无法读取，不会自动重发；可以稍后再请我检查当前页。')
      }
      if (!current()) return
      const starting = localApi.startDocumentMonitor(sessionId)
      monitorStartPendingRef.current = starting
      const monitor = await starting.finally(() => {
        if (monitorStartPendingRef.current === starting) monitorStartPendingRef.current = null
      })
      if (!current()) return
      trialPolicyRef.current = monitor.trial_capture_policy
      monitorGenerationRef.current = monitor.generation
      const binding = (await localApi.deviceBindings()).find((item) => item.device_type === 'document_camera')
      if (!current()) return
      if (!binding) {
        bindingFailure = { code: 'DEVICE_NOT_BOUND', message: '家长尚未绑定高拍仪，请在家长中心选择设备。' }
        throw new Error(bindingFailure.code)
      }
      const devices = await navigator.mediaDevices.enumerateDevices()
      if (!current()) return
      const selected = await resolveBoundVideoDevice(devices, binding.system_id_hash, binding.label, 'document_camera')
      if (!current()) return
      if (!selected) {
        bindingFailure = { code: 'BOUND_DEVICE_NOT_FOUND', message: '未找到已绑定的高拍仪，请检查 USB 连接或让家长核对绑定。' }
        throw new Error(bindingFailure.code)
      }
      setDeviceLabel(selected.device.label || binding.label)
      setFrameDimensions(null)
      const stream = await openDocumentCameraStream(selected.device.deviceId, 'monitor', undefined, binding.system_id_hash)
      if (!current()) {
        stream.getTracks().forEach((track) => track.stop())
        return
      }
      streamRef.current = stream
      const track = stream.getVideoTracks()[0]
      const autofocus = await configureDocumentCameraAutofocus(track)
      if (!current()) return
      const registration = await mediaDeviceRegistration(track)
      if (!current()) return
      registration.capabilities.autofocus = autofocus
      const systemIdHash = selected.method === 'system_id_hash' ? registration.systemIdHash : binding.system_id_hash
      const lease = await localApi.registerDevice(sessionId, 'document_camera', systemIdHash, registration.capabilities)
      if (!current()) {
        await releaseLeases([lease.lease_id], 'monitor_superseded')
        return
      }
      streamRef.current = stream
      leaseRef.current = lease.lease_id
      // A missing endpoint or stale epoch disables only this optional local
      // observation channel. It never interrupts camera or OCR operation.
      const serverGeneration = monitorGenerationRef.current
      if (childId && serverGeneration !== undefined && typeof localApi.documentObservationEpoch === 'function') {
        void localApi.documentObservationEpoch(sessionId).then(({ epoch }) => {
          if (!current() || leaseRef.current !== lease.lease_id || monitorGenerationRef.current !== serverGeneration || !epoch) return
          visibilityRef.current = new DocumentVisibilityChannel(
            { childId, leaseId: lease.lease_id, epoch, generation: serverGeneration },
            (payload) => localApi.recordDocumentObservationSegment(sessionId, payload),
          )
        }).catch(() => undefined)
      }
      if (videoRef.current) {
        videoRef.current.srcObject = stream
        // A stalled play promise must not prevent the first-frame watchdog.
        void videoRef.current.play().catch(() => {
          if (!current()) return
          release('document_monitor_play_failed')
          setStatus('unavailable')
          setReconnectAttempt((value) => value + 1)
          setMessage('高拍仪预览未能启动，正在自动重新连接。')
        })
      }
      if (!current()) return
      track.addEventListener('ended', () => {
        if (!current() || streamRef.current !== stream) return
        release('document_camera_disconnected')
        setStatus('unavailable')
        setReconnectAttempt((value) => value + 1)
        setMessage('高拍仪已断开，正在等待设备恢复并自动重连。')
      }, { once: true })
      let firstFrameConfirmed = false
      const confirmFirstFrame = (): void => {
        const video = videoRef.current
        if (firstFrameConfirmed || !current() || streamRef.current !== stream || !video) return
        if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA || video.videoWidth < 320 || video.videoHeight <= 0) return
        firstFrameConfirmed = true
        if (firstFrameTimerRef.current !== null) {
          window.clearTimeout(firstFrameTimerRef.current)
          firstFrameTimerRef.current = null
        }
        if (firstFramePollRef.current !== null) {
          window.clearInterval(firstFramePollRef.current)
          firstFramePollRef.current = null
        }
        if (firstVideoFrameRef.current !== null && video.cancelVideoFrameCallback) {
          video.cancelVideoFrameCallback(firstVideoFrameRef.current)
          firstVideoFrameRef.current = null
        }
        setFrameDimensions(`${video.videoWidth} × ${video.videoHeight}`)
        setStatus('active')
        setReconnectAttempt(0)
        lastOpenIncident.current = null
        void localApi.confirmDeviceFrame(lease.lease_id, video.videoWidth, video.videoHeight).catch(() => undefined)
        setMessage(analysisActiveRef.current
          ? `已确认 ${video.videoWidth}×${video.videoHeight} 首帧，持续本地监控已开启，等待页面稳定后识别。`
          : `已确认 ${video.videoWidth}×${video.videoHeight} 首帧，当前只显示本地实时预览；进入作业或学习阶段后自动分析页面。`)
      }
      videoRef.current?.addEventListener('loadeddata', confirmFirstFrame, { once: true })
      videoRef.current?.addEventListener('resize', confirmFirstFrame, { once: true })
      videoRef.current?.addEventListener('timeupdate', confirmFirstFrame, { once: true })
      const video = videoRef.current
      if (video?.requestVideoFrameCallback) {
        firstVideoFrameRef.current = video.requestVideoFrameCallback(() => confirmFirstFrame())
      }
      firstFramePollRef.current = window.setInterval(confirmFirstFrame, 100)
      setStatus('starting')
      setMessage('高拍仪已打开，正在等待真实首帧。')
      confirmFirstFrame()
      if (!firstFrameConfirmed) firstFrameTimerRef.current = window.setTimeout(async () => {
        if (!current() || streamRef.current !== stream) return
        const released = await release('document_camera_first_frame_timeout')
        if (streamRef.current !== null) return
        setStatus('unavailable')
        setReconnectAttempt((value) => value + 1)
        setMessage(released
          ? '高拍仪已打开但 12 秒内没有返回可用画面，已释放设备并自动重连。'
          : '高拍仪没有返回可用画面，且设备租约释放尚未确认。')
        void localApi.recordDeviceIncident(
          sessionId, 'document_camera', 'NO_USABLE_VIDEO_FRAME',
          '高拍仪已打开但 12 秒内没有返回可用画面，应用已停止本次读取。'
        ).catch(() => undefined)
      }, 12_000)
    } catch (cause) {
      if (!current()) return
      release('document_monitor_open_failed')
      setStatus('unavailable')
      setReconnectAttempt((value) => value + 1)
      const failure = bindingFailure ?? mediaDeviceFailure(cause, '高拍仪')
      setMessage(failure.message)
      const incident = `${sessionId}:${failure.code}`
      if (lastOpenIncident.current !== incident) {
        lastOpenIncident.current = incident
        void localApi.recordDeviceIncident(sessionId, 'document_camera', failure.code, failure.message).catch(() => undefined)
      }
    } finally {
      if (openingRef.current === opening) openingRef.current = null
      settleOpening()
      if (opening.reopen && runningRef.current) void openCameraRef.current()
    }
  }, [release, releaseLeases, sessionId])
  useLayoutEffect(() => { openCameraRef.current = openCamera }, [openCamera])
  useLayoutEffect(() => registerDocumentMonitorHandoff({
    yieldCamera: async () => {
      const inFlight = openingRef.current?.settled
      const handoff = release('document_monitor_manual_capture')
      if (runningRef.current) {
        setStatus('paused')
        setMessage('正在使用高拍仪拍题，拍题结束后会恢复监控。')
      }
      await inFlight
      // An in-flight register may have completed and queued a release after handoff started.
      return await handoff && await releasePendingRef.current
    },
    resumeCamera: () => { if (runningRef.current) void openCameraRef.current() },
  }), [release])

  useEffect(() => {
    if (!shouldRun || status !== 'unavailable') return
    const media = navigator.mediaDevices
    if (!media?.enumerateDevices) return
    const retry = (): void => { void openCamera() }
    const delay = Math.min(30_000, 1_000 * (2 ** Math.min(reconnectAttempt, 5)))
    const timer = window.setTimeout(retry, delay)
    media.addEventListener?.('devicechange', retry)
    return () => {
      window.clearTimeout(timer)
      media.removeEventListener?.('devicechange', retry)
    }
  }, [openCamera, reconnectAttempt, shouldRun, status])

  useLayoutEffect(() => {
    runningRef.current = shouldRun
    setRevision(null)
    setRecoveryNotice(null)
    unconfirmedCaptureRef.current = false
    setAnalysisBlocked(false)
    setCloudFeedback(null)
    setFrameState('stabilizing')
    setPreviewOrientation(0)
    lastEventAtRef.current = Number.NEGATIVE_INFINITY
    lastFramingGuidanceRef.current = { state: '', at: Number.NEGATIVE_INFINITY }
    if (shouldRun) {
      void openCamera()
    } else {
      setStatus(active ? 'paused' : 'inactive')
      setMessage(active ? '高拍仪监控已暂停。' : '等待学习继续，高拍仪已停止读取。')
    }
    if (!shouldRun) void release('document_monitor_yielded')
  }, [active, openCamera, release, sessionId, shouldRun])

  useLayoutEffect(() => () => { runningRef.current = false; void release('document_monitor_unmounted') }, [release, sessionId])

  useEffect(() => {
    const query = (event: Event): void => {
      const detail = (event as CustomEvent<CameraStatusRequest>).detail
      if (detail?.sessionId === sessionId && detail.device === 'document_camera') detail.resolve(deviceFrameClock.current.read('document_camera', videoRef.current, streamRef.current, runningRef.current, status === 'unavailable' ? 'DOCUMENT_CAMERA_UNAVAILABLE' : null))
    }
    const timer = window.setInterval(() => deviceFrameClock.current.read('document_camera', videoRef.current, streamRef.current, runningRef.current, null), 1_000)
    window.addEventListener(CAMERA_DEVICE_STATUS, query)
    return () => { window.clearInterval(timer); window.removeEventListener(CAMERA_DEVICE_STATUS, query) }
  }, [sessionId, status])

  useEffect(() => {
    let disposed = false
    const control = (event: Event): void => {
      const detail = (event as CustomEvent<CameraDeviceCommand>).detail
      if (detail?.sessionId !== sessionId || detail.device !== 'document_camera' || !active) return
      event.preventDefault()
      const abort = (): void => { runningRef.current = false; setPausedSession(sessionId); void release('document_command_cancelled') }
      detail.signal.addEventListener('abort', abort, { once: true })
      void (async () => {
        try {
          if (disposed || detail.signal.aborted) throw new DOMException('Cancelled', 'AbortError')
          if (detail.command === 'pause') {
            runningRef.current = false
            const released = release('child_paused_document_monitor')
            setPausedSession(sessionId)
            const saved = await saveMonitorChoice('pause')
            if (!await released || saved.status !== 'paused') throw new Error('DOCUMENT_PAUSE_NOT_CONFIRMED')
          } else {
            const generation = generationRef.current
            const saved = await saveMonitorChoice('resume')
            if (disposed || detail.signal.aborted || generationRef.current !== generation) throw new DOMException('Cancelled', 'AbortError')
            if (saved.status !== 'active' && saved.status !== 'not_started') throw new Error('DOCUMENT_RESUME_NOT_SAVED')
            restoredSessionRef.current = sessionId
            runningRef.current = true
            setPausedSession(null)
            await openCameraRef.current()
            await waitForCameraFrame(() => ({ video: videoRef.current, stream: streamRef.current, leased: Boolean(leaseRef.current), generation: generationRef.current }), detail.signal)
          }
          if (disposed || detail.signal.aborted) throw new DOMException('Cancelled', 'AbortError')
          detail.resolve({ device_type: 'document_camera', device_status: detail.command === 'resume' ? 'active' : 'paused', frame_confirmed: detail.command === 'resume',
            lease_confirmed: true, persisted_status: detail.command === 'resume' ? 'active' : 'paused' })
        } catch (error) {
          abort()
          detail.reject(error instanceof Error ? error : new Error('DOCUMENT_CAMERA_ACTION_FAILED'))
        } finally { detail.signal.removeEventListener('abort', abort) }
      })()
    }
    window.addEventListener(CAMERA_DEVICE_COMMAND, control)
    return () => { disposed = true; window.removeEventListener(CAMERA_DEVICE_COMMAND, control) }
  }, [active, release, saveMonitorChoice, sessionId])

  useEffect(() => {
    if (!shouldRun || status !== 'active') return
    const generation = generationRef.current
    const timer = window.setInterval(() => {
      const policy = trialPolicyRef.current, video = videoRef.current
      if (!policy?.enabled || !analysisActiveRef.current || !allowDiagnosticFrameCaptureRef.current || trialCaptureBusyRef.current
          || !runningRef.current || generationRef.current !== generation || Date.now() < nextTrialCaptureAtRef.current
          || !video || !deviceFrameClock.current.read('document_camera', video, streamRef.current, true, null).frame_confirmed) return
      nextTrialCaptureAtRef.current = Date.now() + Math.max(10, policy.interval_seconds) * 1000
      const canvas = document.createElement('canvas')
      canvas.width = video.videoWidth
      canvas.height = video.videoHeight
      const context = canvas.getContext('2d')
      if (!context) return
      context.drawImage(video, 0, 0, canvas.width, canvas.height)
      trialCaptureBusyRef.current = true
      void retainTrialFrame(canvas.toDataURL('image/jpeg', 0.94).split(',', 2)[1], 'periodic')
        .finally(() => { trialCaptureBusyRef.current = false })
    }, 1000)
    return () => window.clearInterval(timer)
  }, [retainTrialFrame, shouldRun, status])

  useEffect(() => {
    if (!shouldRun || status !== 'active') return
    const generation = generationRef.current
    let lastFrameAt = performance.now()
    const timer = window.setInterval(() => {
      if (!runningRef.current || generationRef.current !== generation) return
      const video = videoRef.current
      const usable = video && video.videoWidth >= 320 && video.videoHeight > 0 && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA
      const frame = deviceFrameClock.current.read(
        'document_camera', video, streamRef.current, true, null,
      )
      const frameAge = deviceFrameClock.current.frameAgeMs()
      if (usable && frame.frame_confirmed && frameAge !== null) {
        lastFrameAt = performance.now() - frameAge
      }
      if (performance.now() - lastFrameAt >= 5_000) {
        const now = Date.now()
        if (now - lastStallDiagnosticAtRef.current >= 30_000) { lastStallDiagnosticAtRef.current = now; monitorDiagnostic('frame_stalled', {}, 'warn') }
        release('document_monitor_frame_timeout')
        setStatus('unavailable')
        setReconnectAttempt((value) => value + 1)
        setMessage('高拍仪没有持续返回画面，正在自动重新连接。')
        return
      }
      if (!usable || !frame.frame_confirmed || !analysisActive) {
        cancelFramePreview()
        visibilityRef.current?.pause()
        releaseDocumentCapture()
        return
      }
      const canvas = document.createElement('canvas')
      canvas.width = 320
      canvas.height = Math.max(180, Math.round(video.videoHeight / video.videoWidth * canvas.width))
      const context = canvas.getContext('2d', { willReadFrequently: true })
      if (!context) { cancelFramePreview(); visibilityRef.current?.pause(); return }
      context.drawImage(video, 0, 0, canvas.width, canvas.height)
      const features = extractDocumentFrameFeatures(context.getImageData(0, 0, canvas.width, canvas.height))
      const frameToken = deviceFrameClock.current.frameToken()
      const decision = monitorRef.current.observeFreshFrame(features, Date.now(), frameToken)
      if (!decision) {
        // Until a new decoded frame arrives, neither stability nor the
        // authority to use a previous page can advance.
        visibilityRef.current?.pause()
        releaseDocumentCapture()
        if ((deviceFrameClock.current.frameAgeMs() ?? Infinity) >= 1250) cancelFramePreview()
        if ((deviceFrameClock.current.frameAgeMs() ?? Infinity) >= 1500 && !freshnessInvalidRef.current) {
          freshnessInvalidRef.current = true
          lifecycleEpochRef.current += 1
          freshnessEpochRef.current += 1
          monitorRef.current.invalidateStability()
          setFrameState('stabilizing')
        }
        return
      }
      freshnessInvalidRef.current = false
      if (decision.state !== 'stable' && processingRef.current) {
        visualInterruptionEpochRef.current += 1
        freshnessEpochRef.current += 1
      }
      const matched = monitorRef.current.matchesCapturedPage(features)
      const page = documentCaptureRef.current.page
      const serverGeneration = monitorGenerationRef.current
      if (allowLocalOcrPreview) {
        const lease = leaseRef.current, stream = streamRef.current
        if (frameToken !== null && frameAge !== null && frameAge < 1250 && lease && stream && serverGeneration !== undefined) {
          observePreview({ video, features, motion: decision.motionScore, token: frameToken, generation: serverGeneration,
            cameraKey: JSON.stringify([generation, serverGeneration, lease]),
            available: !processingRef.current && !forceCaptureRef.current && !unconfirmedCaptureRef.current,
            current: () => runningRef.current && analysisActiveRef.current && generationRef.current === generation
              && streamRef.current === stream && leaseRef.current === lease && monitorGenerationRef.current === serverGeneration
              && reviewScopeRef.current.sessionId === sessionId && reviewScopeRef.current.childId === childId })
        } else cancelFramePreview()
      }
      let frameCount: number | undefined
      try { frameCount = video.getVideoPlaybackQuality?.().totalVideoFrames } catch { /* no trustworthy frame counter */ }
      if (page && childId && page.child_id === childId && page.session_id === sessionId
          && serverGeneration !== undefined && typeof frameCount === 'number') {
        visibilityRef.current?.sample({ tickMs: performance.now(), frameCount, generation: serverGeneration,
          pageRevisionId: page.revision_id, qualityOk: decision.state === 'stable' && !processingRef.current,
          pageMatches: matched })
      } else visibilityRef.current?.pause()
      if (!matched) {
        releaseDocumentCapture()
      } else if (childId && documentCaptureRef.current.page && !documentCaptureRef.current.owner) {
        documentCaptureRef.current.owner = publishVoiceDocumentCapture(sessionId, childId, documentCaptureRef.current.page)
      }
      if (decision.changed && decision.state === 'page_changed' && trialPolicyRef.current?.enabled && !trialCaptureBusyRef.current) {
        const snapshot = document.createElement('canvas')
        snapshot.width = video.videoWidth
        snapshot.height = video.videoHeight
        const snapshotContext = snapshot.getContext('2d')
        if (snapshotContext) {
          snapshotContext.drawImage(video, 0, 0, snapshot.width, snapshot.height)
          trialCaptureBusyRef.current = true
          void retainTrialFrame(snapshot.toDataURL('image/jpeg', 0.94).split(',', 2)[1], 'page_changed')
            .finally(() => { trialCaptureBusyRef.current = false })
        }
      }
      setFrameState(decision.state)
      persistEvent(decision, features)
      if (decision.changed && decision.state === 'no_page') {
        const now = Date.now()
        const previous = lastFramingGuidanceRef.current
        if (now - previous.at >= 20_000) {
          lastFramingGuidanceRef.current = { state: decision.state, at: now }
          window.dispatchEvent(new CustomEvent('hsc-agent-intervention', {
            detail: {
              message: '高拍仪还没有看到完整页面，请把书本或作业放到画面中央。',
              sourceType: 'document_monitor_framing',
              sessionId,
            }
          }))
        }
      }
      if (decision.captureRecommended || (forceCaptureRef.current && decision.state === 'stable')) {
        void captureStablePage()
      } else if (decision.changed && !processingRef.current && !analysisBlocked) {
        setMessage(frameStateMessage(decision.state))
      }
    }, 500)
    return () => window.clearInterval(timer)
  }, [allowLocalOcrPreview, analysisActive, analysisBlocked, cancelFramePreview, captureStablePage, childId, observePreview, persistEvent, release, releaseDocumentCapture, retainTrialFrame, sessionId, shouldRun, status])

  useEffect(() => {
    if (!shouldRun || status !== 'active') return
    const generation = generationRef.current
    let renewing = false
    const timer = window.setInterval(() => {
      if (!runningRef.current || generationRef.current !== generation || renewing) return
      const lease = leaseRef.current
      if (lease) {
        renewing = true
        void localApi.renewDeviceLease(lease).catch(() => {
          if (!runningRef.current || generationRef.current !== generation || leaseRef.current !== lease) return
          release('document_monitor_lease_failed')
          setStatus('unavailable')
          setReconnectAttempt((value) => value + 1)
          setMessage('高拍仪连接续期失败，正在自动重新连接。')
        }).finally(() => { renewing = false })
      }
    }, 60_000)
    return () => window.clearInterval(timer)
  }, [release, shouldRun, status])

  useEffect(() => {
    const pause = (): void => {
      runningRef.current = false
      release('voice_privacy_pause')
      setPausedSession(sessionId)
      void saveMonitorChoice('pause').catch(() => undefined)
    }
    window.addEventListener('hsc-privacy-pause', pause)
    return () => {
      window.removeEventListener('hsc-privacy-pause', pause)
    }
  }, [release, saveMonitorChoice, sessionId])

  useEffect(() => {
    const control = (event: Event): void => {
      const detail = (event as CustomEvent<{ sessionId?: string; action?: 'pause' | 'resume' | 'inspect' | 'review'; deferReview?: boolean; signal?: AbortSignal; report?: (output: DocumentInspectionOutput) => void; resolve?: (handled: boolean) => void }>).detail
      if (event.defaultPrevented || detail?.sessionId !== sessionId || !active) return
      if (!detail.action || !['pause', 'resume', 'inspect', 'review'].includes(detail.action)) return
      event.preventDefault()
      if (detail.action === 'pause' || detail.action === 'resume') {
        void requestCameraDevice(sessionId, 'document_camera', detail.action, detail.signal)
          .then(() => detail.resolve?.(true), () => detail.resolve?.(false))
        return
      }
      if (detail.action === 'inspect' || detail.action === 'review') {
        if (!runningRef.current || status !== 'active') { detail.resolve?.(false); return }
        if (!analysisActiveRef.current) {
          setMessage('当前只显示本地实时预览；进入作业或学习阶段后才能识别页面。')
          detail.resolve?.(false)
          return
        }
        if (pendingInspectRef.current || detail.signal?.aborted) { detail.resolve?.(false); return }
        if (detail.action === 'review' && !detail.deferReview && !reviewRef.current) { detail.resolve?.(false); return }
        if (detail.action === 'review' && revision && !processingRef.current) {
          const features = readLiveFeatures()
          const samePage = features && monitorRef.current.matchesCapturedPage(features)
          if (samePage) {
            detail.report?.({ artifact_id: revision.artifact_id, revision_id: revision.revision_id,
              page_status: cloudFeedback?.blocked ? 'blocked' : revision.extracted_text.trim() ? 'text_available' : 'empty',
              response_text: '已准备本页的文字核对，请说需要修改哪里；文字核对还不代表题目已经做对。' })
            if (!detail.deferReview) reviewRef.current?.(revision)
            detail.resolve?.(true)
            return
          }
        }
        unconfirmedCaptureRef.current = false
        forceCaptureRef.current = true
        setMessage('请移开手并保持页面稳定，我会立即检查。')
        const finish = (success: boolean, output?: DocumentInspectionOutput): void => { clearTimeout(watchdog); detail.signal?.removeEventListener('abort', abort); if (output) detail.report?.(output); detail.resolve?.(success) }
        const abort = (): void => {
          forceCaptureRef.current = false; pendingInspectRef.current = null
          runningRef.current = false; setPausedSession(sessionId)
          void release('document_inspect_cancelled')
          finish(false)
        }
        const watchdog = setTimeout(() => {
          if (pendingInspectRef.current !== finish) return
          pendingInspectRef.current = null
          forceCaptureRef.current = false
          finish(false)
          setMessage('这次页面检查尚未取得结果；你可以继续说话，稍后再请我检查。')
        }, DOCUMENT_INSPECTION_WATCHDOG_MS)
        pendingInspectRef.current = Object.assign(finish, { deferReview: detail.deferReview === true })
        detail.signal?.addEventListener('abort', abort, { once: true })
        return
      }
    }
    window.addEventListener('hsc-document-monitor-voice-action', control)
    return () => window.removeEventListener('hsc-document-monitor-voice-action', control)
  }, [active, cloudFeedback, onReviewRevision, readLiveFeatures, release, revision, sessionId, status])

  const togglePause = (): void => {
    if (manualPaused) {
      const generation = generationRef.current
      void saveMonitorChoice('resume').then(saved => {
        if (generationRef.current !== generation) return
        if (saved.status !== 'active' && saved.status !== 'not_started') throw new Error('DOCUMENT_RESUME_NOT_SAVED')
        restoredSessionRef.current = sessionId
        setPausedSession(null)
      }).catch(() => {
        if (generationRef.current === generation) setMessage('高拍仪仍保持暂停，恢复设置尚未保存，请重试。')
      })
    } else {
      runningRef.current = false
      release('child_paused_document_monitor')
      setPausedSession(sessionId)
      void saveMonitorChoice('pause').catch(() => undefined)
    }
  }

  return <section className={`continuous-document-monitor is-${status}`} aria-label="高拍仪纸面识别">
    {diagnosticNotice && <p role="status" className="report-uncertainty">{diagnosticNotice}</p>}
    <div className="continuous-document-heading">
      <FileScan size={21} aria-hidden="true" />
      <div><strong>纸面识别</strong><span>{message}</span></div>
      {(active || status === 'unavailable') && <div className="document-camera-connection-actions">
        {active && <button className="secondary-button" title={manualPaused ? '继续书写监控' : '暂停书写监控'} onClick={togglePause}>
          {manualPaused ? <Play size={17} aria-hidden="true" /> : <Pause size={17} aria-hidden="true" />}
          {manualPaused ? '继续高拍仪' : '暂停高拍仪'}
        </button>}
        {status === 'unavailable' && <button className="secondary-button" title="重新连接高拍仪" onClick={() => void openCamera()}>
          <RotateCw size={17} aria-hidden="true" />重新连接高拍仪
        </button>}
      </div>}
    </div>
    <div className="continuous-document-body">
      <div className="device-live-video-frame">
        <video
          ref={videoRef}
          autoPlay
          muted
          playsInline
          aria-label="高拍仪实时画面"
          data-auto-orientation={previewOrientation}
          style={{
            transform: `rotate(${previewOrientation}deg) scale(${previewOrientation === 90 || previewOrientation === 270 ? 0.5625 : 1})`,
          }}
        />
        <span className={`device-frame-badge is-${status}`}>{status === 'active' ? '实时画面' : status === 'starting' ? '等待首帧' : status === 'unavailable' ? '画面不可用' : status === 'paused' ? '已暂停' : '未读取'}</span>
        {previewOrientation !== 0 && <span className="device-orientation-badge">已自动转正 {previewOrientation}°</span>}
      </div>
      <div><b>{status === 'active' ? frameStateLabel(frameState) : status === 'paused' ? '已暂停' : status === 'starting' ? '正在连接并验证画面' : status === 'unavailable' ? '设备不可用' : '等待任务开始'}</b>
        <div className="device-live-facts" aria-label="高拍仪连接详情">
          <span title={deviceLabel}>设备：{deviceLabel}</span>
          <span>画面：{frameDimensions ?? '尚未确认'}</span>
          <span>重连：{reconnectAttempt > 0 ? `第 ${reconnectAttempt} 次` : '无需重连'}</span>
        </div>
        {recoveryNotice && <p>{recoveryNotice}</p>}
        <small>{analysisActive && status === 'active' && !analysisBlocked
          ? '每半秒检查画面；稳定后识别，通常间隔至少15秒；被中断时会尽快重试，不变时约5分钟复查。'
          : !analysisActive ? '进入作业或学习阶段后才会自动识别页面。'
            : analysisBlocked ? '页面识别暂受阻，原因见上方提示。' : '设备连接并恢复页面识别后才会自动检查。'}</small>
        <small>多帧遮挡补全暂不可用，当前使用单帧识别。</small>
        <small>{allowDiagnosticFrameCapture ? '原视频不上传；合格页内图按配置解析，识别内容仍需核对。' : '当前仅在本机识别页面，不保留原始画面或上传云端；识别内容仍需核对。'}</small>
        {revision?.extracted_text && <p>{revision.extracted_text.slice(0, 180)}</p>}
        {revision && <small>第{revision.revision_index}版 · 识别置信度 {Math.round(revision.ocr_confidence * 100)}%</small>}
        {localPreviewVisible && (localPreviewHistory.items.length > 0 || localPreviewStatus !== 'idle' || localPreviewHistory.unavailable) &&
          <section className="document-ocr-preview-history" aria-label="本机待核对预览">
            <strong>本机待核对预览</strong>
            <small>只读取拍到的文字。预览可能缺题或混读，须对照原卷核对。</small>
            {localPreviewStatus === 'reading' && <small role="status">正在本机读取可见文字…</small>}
            {localPreviewStatus === 'interrupted' && <small role="status">画面变化，预览已中断。可以刷新查看已保存的预览。</small>}
            {localPreviewStatus === 'unconfirmed' && <small role="status">暂未确认保存。可以刷新查看已保存的预览。</small>}
            {localPreviewHistory.unavailable && <small role="status">暂时读不到已保存的预览。</small>}
            {(localPreviewHistory.unavailable || localPreviewStatus === 'unconfirmed' || localPreviewStatus === 'interrupted') &&
              <button className="secondary-button" onClick={refreshLocalPreviews}>刷新预览记录</button>}
            {localPreviewHistory.items.map(item => <details key={item.preview_id}>
              <summary><time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString()}</time> · {item.question_count} 个题目候选</summary>
              <div className="document-ocr-preview-text">{item.text || '这次未读出可核对的文字。'}</div>
            </details>)}
          </section>}
        {cloudFeedback && <div aria-label="云端页面解析">
          <strong>{cloudFeedback.blocked ? '云端解析受阻' : '云端文字待核对'}</strong>
          <p>{cloudFeedback.message}</p>
          {cloudFeedback.text && <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{cloudFeedback.text.slice(0, 600)}</p>}
          {cloudFeedback.pageInkUploaded && <small>本次上传了页内二值文字图。识别结果不代表已经掌握或答错。</small>}
        </div>}
      </div>
      <div className="continuous-document-actions">
        {analysisActive && revision?.extracted_text && onReviewRevision && <button className="secondary-button" disabled={processing} onClick={() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', { cancelable: true, detail: { sessionId, action: 'review' } }))}>核对这页并提问</button>}
        {status === 'active' && analysisActive && <button className="secondary-button" disabled={processing} onClick={() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', { cancelable: true, detail: { sessionId, action: 'inspect' } }))}>{processing ? '正在识别' : '检查当前页'}</button>}
      </div>
    </div>
  </section>
}

function normalizePageOrientation(value: number | undefined): 0 | 90 | 180 | 270 {
  return value === 90 || value === 180 || value === 270 ? value : 0
}

function frameStateLabel(state: DocumentMonitorState): string {
  return ({ no_page: '等待放好书本或作业纸', stabilizing: '等待页面稳定', writing: '正在书写', occluded: '持续观察中', page_changed: '检测到翻页或大幅变化', stable: '页面稳定', capture_failed: '本地识别失败' })[state]
}

function frameStateMessage(state: DocumentMonitorState): string {
  return ({ no_page: '没有看到完整页面，请把作业纸放到高拍仪下。', stabilizing: '页面还在移动，稳定后再识别。', writing: '看到你正在书写，我先不打扰。', occluded: '画面暂时有遮挡，监控仍在继续，清晰后会自动识别。', page_changed: '检测到页面变化，正在等待新页面稳定。', stable: '页面已经稳定。', capture_failed: '本地识别失败，监控仍在继续。' })[state]
}
