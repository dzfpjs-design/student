import { useEffect, useRef, useState } from 'react'
import { Camera, Crop, FileScan, RotateCw, ScanText, X } from 'lucide-react'
import { ApiError, localApi } from './api'
import { DocumentFrameMonitor, documentFrameDiagnosticBands, extractDocumentFrameFeatures } from './documentFrameMonitor'
import { configureDocumentCameraAutofocus, mediaDeviceFailure, mediaDeviceRegistration, openDocumentCameraStream, resolveBoundVideoDevice } from './mediaDevices'
import type { OcrArtifact } from './types'
import { emitCaptureReceipt, privacyStopPending, type CaptureReceipt } from './deviceCommandReceipt'
import { CAMERA_DEVICE_STATUS, CameraFrameClock, type CameraStatusRequest } from './cameraDeviceRuntime'
import { reserveDocumentCameraForCapture } from './documentCameraHandoff'
import { selectOcrTextCandidate } from './tutorOcrEvidence'

type CropMargins = { top: number; right: number; bottom: number; left: number }
export type ParentDocumentCapturePort = {
  acquire: (systemIdHash: string, capabilities: Record<string, unknown>) => Promise<{ lease_id: string; label?: string }>
  renew: (leaseId: string) => Promise<unknown>
  release: (leaseId: string, reason: string) => Promise<unknown>
  recognize: (imageBase64: string, leaseId: string, transform?: { rotation: number; crop: CropMargins }) => Promise<OcrArtifact>
  recognizeFile?: (imageBase64: string, transform: { rotation: number; crop: CropMargins }, clientActionId: string) => Promise<OcrArtifact>
}
const EMPTY_CROP: CropMargins = { top: 0, right: 0, bottom: 0, left: 0 }
const MAX_OCR_PIXELS = 4160 * 3120
const MAX_OCR_BASE64_LENGTH = 24_000_000

class CaptureTooLargeError extends Error {}

function encodeBoundedJpeg(canvas: HTMLCanvasElement): string {
  for (const quality of [0.94, 0.8]) {
    const image = canvas.toDataURL('image/jpeg', quality)
    const prefix = 'data:image/jpeg;base64,'
    if (!image.startsWith(prefix) || image.length === prefix.length) throw new Error('CAPTURE_IMAGE_UNAVAILABLE')
    if (image.length - prefix.length <= MAX_OCR_BASE64_LENGTH) return image
  }
  throw new CaptureTooLargeError('图片过大，暂时无法识别。请调低高拍仪分辨率后重拍，或导入较小的图片。')
}

function recordDocumentDiagnostic(event: string, level: 'debug' | 'info' | 'warn' | 'error' = 'info', details?: Record<string, unknown>): void {
  void window.desktop?.recordRendererDiagnostic?.({ event, level, details }).catch(() => undefined)
}

type DocumentCameraCaptureProps = {
  sessionId: string
  commandId?: string
  onText?: (text: string, artifact: OcrArtifact) => void
  onArtifact?: (artifact: OcrArtifact) => void
  onGuidance?: (message: string) => void
  parentPort?: ParentDocumentCapturePort
  automation?: 'manual' | 'capture_and_recognize'
}

export function DocumentCameraCapture(props: DocumentCameraCaptureProps): React.JSX.Element {
  return <ScopedDocumentCameraCapture key={JSON.stringify([props.sessionId, props.commandId ?? null])} {...props} />
}

function ScopedDocumentCameraCapture({ sessionId, onText, onArtifact, onGuidance, parentPort, commandId, automation = 'manual' }: DocumentCameraCaptureProps): React.JSX.Element {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const deviceFrameClock = useRef(new CameraFrameClock())
  const leaseRef = useRef<string | null>(null)
  const cameraReservation = useRef<ReturnType<typeof reserveDocumentCameraForCapture>>(null)
  const cameraLeasePending = useRef<Promise<void> | null>(null)
  const confirmedFrameLeaseRef = useRef<string | null>(null)
  const captureSourceRef = useRef<{ kind: 'camera'; leaseId: string | null } | { kind: 'file' } | null>(null)
  const fileRequestRef = useRef<{ version: number; id: string } | null>(null)
  const openGenerationRef = useRef(0)
  const privacyStoppedRef = useRef(privacyStopPending(sessionId))
  const mountedRef = useRef(true)
  const terminalReceiptRef = useRef(false)
  const report = (status: CaptureReceipt['status'], errorCode?: string, artifactId?: string, ocrStatus?: CaptureReceipt['ocrStatus']): void => {
    if (!commandId || terminalReceiptRef.current) return
    if (['recognized', 'failed', 'cancelled'].includes(status)) terminalReceiptRef.current = true
    emitCaptureReceipt({ sessionId, commandId, status, errorCode, artifactId, ocrStatus })
  }
  const openingRef = useRef(false)
  const openingOwnerRef = useRef<number | null>(null)
  const autoFrameIntervalRef = useRef<number | null>(null)
  const autoOpenTimerRef = useRef<number | null>(null)
  const frameMonitorRef = useRef(new DocumentFrameMonitor({ stableSamples: 3 }))
  const lastGuidanceRef = useRef({ kind: '', at: 0 })
  const occludedSinceRef = useRef<number | null>(null)
  const lastOcclusionDiagnosticRef = useRef<number | null>(null)
  const frameDiagnosticRef = useRef<{ state: string; at: number } | null>(null)
  const framingRetryCountRef = useRef(0)
  const autofocusSettlingUntilRef = useRef(0)
  const autoRecognizedCaptureRef = useRef<number | null>(null)
  const captureVersionRef = useRef(0)
  const ocrOwnerRef = useRef<symbol | null>(null)
  const [captureVersion, setCaptureVersion] = useState(0)
  const [deviceLabel, setDeviceLabel] = useState('')
  const [open, setOpen] = useState(false)
  const [captured, setCaptured] = useState<string | null>(null)
  const [rotation, setRotation] = useState(0)
  const [crop, setCrop] = useState<CropMargins>(EMPTY_CROP)
  const [busy, setBusy] = useState(false)
  const [opening, setOpening] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  const reviseCapture = (): number => {
    const version = ++captureVersionRef.current
    setCaptureVersion(version)
    return version
  }

  useEffect(() => {
    const read = (): ReturnType<CameraFrameClock['read']> => deviceFrameClock.current.read('document_camera', videoRef.current, streamRef.current, open,
      !open && !opening && message ? 'DOCUMENT_CAPTURE_NOT_READY' : null)
    const query = (event: Event): void => {
      const detail = (event as CustomEvent<CameraStatusRequest>).detail
      if (detail?.sessionId === sessionId && detail.device === 'document_camera') detail.resolve(read())
    }
    const timer = window.setInterval(read, 1_000)
    window.addEventListener(CAMERA_DEVICE_STATUS, query)
    return () => { window.clearInterval(timer); window.removeEventListener(CAMERA_DEVICE_STATUS, query) }
  }, [message, open, opening, sessionId])

  useEffect(() => {
    window.dispatchEvent(new CustomEvent('hsc-document-capture-presence', { detail: { sessionId, active: true } }))
    return () => {
      window.dispatchEvent(new CustomEvent('hsc-document-capture-presence', { detail: { sessionId, active: false } }))
    }
  }, [sessionId])

  const release = (reason = 'renderer_closed', keepParentLease = false): void => {
    if (reason === 'child_closed' || reason === 'voice_privacy_pause') report('cancelled', 'DEVICE_CAPTURE_CANCELLED')
    else if (reason === 'device_disconnected' || reason === 'open_failed') report('failed', 'DEVICE_CAPTURE_UNAVAILABLE')
    if (autoOpenTimerRef.current !== null) {
      window.clearTimeout(autoOpenTimerRef.current)
      autoOpenTimerRef.current = null
    }
    if (autoFrameIntervalRef.current !== null) {
      window.clearInterval(autoFrameIntervalRef.current)
      autoFrameIntervalRef.current = null
    }
    frameMonitorRef.current.reset()
    occludedSinceRef.current = null
    lastOcclusionDiagnosticRef.current = null
    frameDiagnosticRef.current = null
    autofocusSettlingUntilRef.current = 0
    openGenerationRef.current += 1
    const stream = streamRef.current
    streamRef.current = null
    stream?.getTracks().forEach((track) => track.stop())
    const reservation = cameraReservation.current
    cameraReservation.current = null
    if (leaseRef.current && !keepParentLease) {
      void (parentPort ? parentPort.release(leaseRef.current, reason) : localApi.releaseDeviceLease(leaseRef.current, reason, AbortSignal.timeout(8_000)))
        .catch(() => undefined).finally(() => reservation?.release())
      leaseRef.current = null
    } else if (reservation && cameraLeasePending.current) {
      void cameraLeasePending.current.finally(reservation.release)
    } else reservation?.release()
    if (videoRef.current) videoRef.current.srcObject = null
    setOpen(false)
  }

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      release()
      queueMicrotask(() => { if (!mountedRef.current) report('cancelled', 'DEVICE_CAPTURE_INTERRUPTED') })
    }
  }, [])

  useEffect(() => {
    const pause = (): void => {
      privacyStoppedRef.current = true
      release('voice_privacy_pause')
    }
    window.addEventListener('hsc-privacy-pause', pause)
    return () => window.removeEventListener('hsc-privacy-pause', pause)
  }, [])

  const openCamera = async (): Promise<void> => {
    if (openingRef.current || privacyStoppedRef.current || !mountedRef.current || (commandId && terminalReceiptRef.current)) return
    recordDocumentDiagnostic('document.capture.open_requested', 'info', { automation, parent_port: Boolean(parentPort) })
    openingRef.current = true
    setOpening(true)
    release('switch_device'); setMessage(null)
    const generation = ++openGenerationRef.current
    openingOwnerRef.current = generation
    const isCurrent = (): boolean => openGenerationRef.current === generation
    if (!navigator.mediaDevices?.getUserMedia) {
      const failure = { code: 'MEDIA_API_UNAVAILABLE', message: '当前环境没有标准摄像头接口。' }
      setMessage(`${failure.message}请使用图片导入。`)
      onGuidance?.(`${failure.message}请让家长检查高拍仪连接。`)
      if (!parentPort) void localApi.recordDeviceIncident(sessionId, 'document_camera', failure.code, failure.message).catch(() => undefined)
      recordDocumentDiagnostic('document.capture.open_failed', 'error', { code: failure.code })
      report('failed', failure.code)
      openingRef.current = false
      openingOwnerRef.current = null
      setOpening(false)
      return
    }
    try {
      const binding = (await localApi.deviceBindings()).find((item) => item.device_type === 'document_camera')
      if (!isCurrent()) return
      if (!binding) {
        const message = '家长尚未绑定高拍仪，请先在家长中心完成设备绑定。'
        setMessage(message)
        onGuidance?.(message)
        if (!parentPort) void localApi.recordDeviceIncident(sessionId, 'document_camera', 'DEVICE_NOT_BOUND', message).catch(() => undefined)
        recordDocumentDiagnostic('document.capture.open_failed', 'warn', { code: 'DEVICE_NOT_BOUND' })
        report('failed', 'DEVICE_NOT_BOUND')
        return
      }
      const available = await navigator.mediaDevices.enumerateDevices()
      if (!isCurrent()) return
      const selected = await resolveBoundVideoDevice(available, binding.system_id_hash, binding.label, 'document_camera')
      if (!isCurrent()) return
      if (!selected) {
        const message = `未找到已绑定的高拍仪“${binding.label}”，请检查连接或让家长重新绑定。`
        setMessage(message)
        onGuidance?.(commandId ? '我没有找到高拍仪，请检查连接。接好后请再说“重新拍题”。' : '我没有找到高拍仪，请检查连接。接好后我会自动继续。')
        if (!parentPort) void localApi.recordDeviceIncident(sessionId, 'document_camera', 'BOUND_DEVICE_NOT_FOUND', message).catch(() => undefined)
        recordDocumentDiagnostic('document.capture.open_failed', 'warn', { code: 'BOUND_DEVICE_NOT_FOUND' })
        report('failed', 'BOUND_DEVICE_NOT_FOUND')
        return
      }
      if (!parentPort) {
        const reservation = reserveDocumentCameraForCapture()
        cameraReservation.current = reservation
        if (!reservation || !await reservation.ready) {
          if (!isCurrent()) return
          throw new DOMException('Document camera release was not confirmed', 'CameraStartPendingError')
        }
        if (!isCurrent()) return
      }
      const stream = await openDocumentCameraStream(selected.device.deviceId, 'capture', undefined, binding.system_id_hash)
      if (!isCurrent()) {
        stream.getTracks().forEach((track) => track.stop())
        return
      }
      streamRef.current = stream
      const track = stream.getVideoTracks()[0]
      const autofocus = await configureDocumentCameraAutofocus(track)
      if (!isCurrent()) {
        stream.getTracks().forEach((item) => item.stop())
        return
      }
      if (autofocus.applied) autofocusSettlingUntilRef.current = Date.now() + 800
      const registration = await mediaDeviceRegistration(track)
      registration.capabilities.autofocus = autofocus
      if (!isCurrent()) {
        stream.getTracks().forEach((item) => item.stop())
        return
      }
      const effectiveSystemIdHash = selected.method === 'system_id_hash'
        ? registration.systemIdHash
        : binding.system_id_hash
      const attaching = (async () => {
        const lease = parentPort
          ? await parentPort.acquire(effectiveSystemIdHash, registration.capabilities)
          : await localApi.registerDevice(sessionId, 'document_camera', effectiveSystemIdHash, registration.capabilities)
        if (isCurrent()) {
          leaseRef.current = lease.lease_id
          return lease
        }
        stream.getTracks().forEach((item) => item.stop())
        await (parentPort ? parentPort.release(lease.lease_id, 'attach_superseded')
          : localApi.releaseDeviceLease(lease.lease_id, 'attach_superseded', AbortSignal.timeout(8_000))).catch(() => undefined)
        return null
      })()
      const settledLease = attaching.then(() => undefined, () => undefined)
      cameraLeasePending.current = settledLease
      const lease = await attaching.finally(() => {
        if (cameraLeasePending.current === settledLease) cameraLeasePending.current = null
      })
      if (!lease || !isCurrent()) return
      confirmedFrameLeaseRef.current = null
      track.addEventListener('ended', () => {
        if (streamRef.current !== stream) return
        release('device_disconnected')
        const message = `已绑定的高拍仪“${binding.label}”已断开，请重新连接后再打开。`
        setMessage(message)
        if (!parentPort) void localApi.recordDeviceIncident(sessionId, 'document_camera', 'DEVICE_DISCONNECTED', message).catch(() => undefined)
      }, { once: true })
      setDeviceLabel(('label' in lease ? lease.label : undefined) || binding.label)
      setOpen(true)
      recordDocumentDiagnostic('document.capture.camera_ready', 'info', {
        selection_method: selected.method,
        autofocus_applied: autofocus.applied,
        width: track.getSettings?.().width,
        height: track.getSettings?.().height,
      })
    } catch (cause) {
      if (!isCurrent()) return
      release('open_failed')
      const parentApiMessage = parentPort ? semanticParentCaptureMessage(cause) : null
      if (parentApiMessage) {
        setMessage(`高拍仪暂不可用：${parentApiMessage}`)
        onGuidance?.(`高拍仪暂时不可用。${parentApiMessage}`)
        recordDocumentDiagnostic('document.capture.open_failed', 'error', { code: 'PARENT_CAPTURE_UNAVAILABLE' })
        return
      }
      const failure = mediaDeviceFailure(cause, '高拍仪')
      setMessage(`${failure.message}可以先使用图片导入。`)
      onGuidance?.(`${failure.message}${commandId ? '请让家长检查连接，设备恢复后请再说“重新拍题”。' : '请让家长检查连接，我会在设备恢复后继续。'}`)
      if (!parentPort) void localApi.recordDeviceIncident(sessionId, 'document_camera', failure.code, failure.message).catch(() => undefined)
      recordDocumentDiagnostic('document.capture.open_failed', 'error', { code: failure.code })
    } finally {
      if (openingOwnerRef.current === generation) {
        openingRef.current = false
        openingOwnerRef.current = null
        setOpening(false)
      }
    }
  }

  useEffect(() => {
    if (automation !== 'capture_and_recognize') return
    // Deferring the initial open makes this effect safe under React StrictMode's
    // development mount-cleanup-remount cycle. Otherwise the discarded first
    // request can hold openingRef long enough to suppress the real second open.
    autoOpenTimerRef.current = window.setTimeout(() => {
      autoOpenTimerRef.current = null
      void openCamera()
    }, 0)
    return () => {
      if (autoOpenTimerRef.current !== null) {
        window.clearTimeout(autoOpenTimerRef.current)
        autoOpenTimerRef.current = null
      }
    }
  }, [automation])

  useEffect(() => {
    if (automation !== 'capture_and_recognize' || !navigator.mediaDevices?.addEventListener) return
    const resumeAfterReconnect = (): void => {
      if (!openingRef.current && !streamRef.current && !captured) void openCamera()
    }
    navigator.mediaDevices.addEventListener('devicechange', resumeAfterReconnect)
    return () => navigator.mediaDevices.removeEventListener('devicechange', resumeAfterReconnect)
  }, [automation, captured])

  useEffect(() => {
    const video = videoRef.current
    const stream = streamRef.current
    if (!open || !video || !stream) return
    const generation = openGenerationRef.current
    video.srcObject = stream
    void video.play().catch((cause) => {
      if (openGenerationRef.current !== generation || streamRef.current !== stream) return
      release('open_failed')
      const failure = mediaDeviceFailure(cause, '高拍仪')
      setMessage(`${failure.message}可以先使用图片导入。`)
      if (!parentPort) void localApi.recordDeviceIncident(sessionId, 'document_camera', failure.code, failure.message).catch(() => undefined)
    })
  }, [open, sessionId])

  const capture = (snapshot?: HTMLCanvasElement): void => {
    const video = videoRef.current
    if (!video || video.videoWidth < 320 || video.videoHeight <= 0) {
      setMessage('视频画面还没有准备好，请稍后再拍。')
      recordDocumentDiagnostic('document.capture.frame_not_ready', 'warn')
      return
    }
    const sourceWidth = snapshot?.width ?? video.videoWidth
    const sourceHeight = snapshot?.height ?? video.videoHeight
    const scale = Math.min(1, Math.sqrt(MAX_OCR_PIXELS / (sourceWidth * sourceHeight)))
    const canvas = snapshot && scale === 1 ? snapshot : document.createElement('canvas')
    let image: string
    try {
      if (canvas !== snapshot) {
        canvas.width = Math.floor(sourceWidth * scale)
        canvas.height = Math.floor(sourceHeight * scale)
        if (canvas.width < 1 || canvas.height < 1) throw new Error('CAPTURE_DIMENSIONS_UNAVAILABLE')
        const context = canvas.getContext('2d')
        if (!context) throw new Error('CAPTURE_CONTEXT_UNAVAILABLE')
        context.drawImage(snapshot ?? video, 0, 0, canvas.width, canvas.height)
      }
      image = encodeBoundedJpeg(canvas)
    } catch (cause) {
      if (autoFrameIntervalRef.current !== null) {
        window.clearInterval(autoFrameIntervalRef.current)
        autoFrameIntervalRef.current = null
      }
      setMessage(cause instanceof CaptureTooLargeError ? cause.message : '暂时无法拍摄画面，请稍后重试或导入图片。')
      recordDocumentDiagnostic('document.capture.frame_blocked', 'warn')
      return
    }
    reviseCapture()
    captureSourceRef.current = { kind: 'camera', leaseId: leaseRef.current }
    setCaptured(image)
    recordDocumentDiagnostic('document.capture.frame_captured', 'info', { width: canvas.width, height: canvas.height })
    setRotation(0); setCrop(EMPTY_CROP); setMessage('已拍摄。请检查是否完整、清楚，再识别文字。')
    // The parent OCR endpoint still needs this active capture permit. Stop the
    // physical tracks now; retire the permit only after its request settles.
    release('capture_completed', Boolean(parentPort))
    report('captured')
  }

  const speakGuidance = (kind: string, guidance: string, intervalMs = 12_000): void => {
    const now = Date.now()
    if (lastGuidanceRef.current.kind === kind && now - lastGuidanceRef.current.at < intervalMs) return
    lastGuidanceRef.current = { kind, at: now }
    onGuidance?.(guidance)
  }

  const analysisUnavailable = (): void => {
    // A temporary read failure is not evidence that the picture is stable.
    // Start stability over if analysis becomes available on a later frame.
    frameMonitorRef.current.reset()
    const now = Date.now(), last = frameDiagnosticRef.current
    if (!last || last.state !== 'analysis_unavailable' || now - last.at >= 30_000) {
      frameDiagnosticRef.current = { state: 'analysis_unavailable', at: now }
      recordDocumentDiagnostic('document.capture.frame_not_ready', 'warn')
    }
    const guidance = '暂时无法判断画面是否稳定，自动拍摄已暂停并会继续重试。请检查画面后手动拍摄；若无法拍摄，可以导入图片。'
    setMessage(guidance)
    speakGuidance('frame_analysis_unavailable', guidance, 30_000)
  }

  const inspectAutomatedFrame = (): void => {
    const video = videoRef.current
    if (!video || video.videoWidth < 320 || captured || streamRef.current === null) return
    if (Date.now() < autofocusSettlingUntilRef.current) {
      setMessage('高拍仪正在自动对焦，画面清晰后会自动拍摄。')
      return
    }
    const frame = deviceFrameClock.current.read('document_camera', video, streamRef.current, true, null)
    const frameToken = deviceFrameClock.current.frameToken()
    const frameAge = deviceFrameClock.current.frameAgeMs()
    if (!frame.frame_confirmed || frameToken === null || frameAge === null || frameAge >= 1500) {
      frameMonitorRef.current.invalidateStability()
      const guidance = '暂时无法确认高拍仪画面持续更新，自动拍摄已暂停。请检查连接或稍后重试，也可以检查画面后手动拍摄或导入图片。'
      setMessage(guidance)
      speakGuidance('frame_freshness_unavailable', guidance, 30_000)
      return
    }
    const canvas = document.createElement('canvas')
    canvas.width = 320
    canvas.height = Math.max(180, Math.round(video.videoHeight / video.videoWidth * canvas.width))
    let features: ReturnType<typeof extractDocumentFrameFeatures>
    try {
      const context = canvas.getContext('2d', { willReadFrequently: true })
      if (!context || typeof context.getImageData !== 'function') throw new Error('FRAME_ANALYSIS_UNAVAILABLE')
      context.drawImage(video, 0, 0, canvas.width, canvas.height)
      features = extractDocumentFrameFeatures(context.getImageData(0, 0, canvas.width, canvas.height))
    } catch {
      analysisUnavailable()
      return
    }
    const decision = frameMonitorRef.current.observeFreshFrame(features, Date.now(), frameToken)
    if (!decision) return
    const now = Date.now(), last = frameDiagnosticRef.current
    if (!last || last.state !== decision.state || now - last.at >= 30_000) {
      frameDiagnosticRef.current = { state: decision.state, at: now }
      recordDocumentDiagnostic('document.capture.frame_state', 'info', {
        frame_state: decision.state, ...documentFrameDiagnosticBands(decision, features),
      })
    }
    if (decision.state === 'occluded') {
      occludedSinceRef.current ??= now
      setMessage('画面暂时被手或笔遮挡，正在持续观察，清晰后会自动识别。')
      if (now - occludedSinceRef.current >= 8_000) {
        speakGuidance('occluded', '高拍仪画面可能被遮挡，请暂时移开手和物品，调整好题目后保持不动。', 30_000)
      }
      if (lastOcclusionDiagnosticRef.current === null || now - lastOcclusionDiagnosticRef.current >= 30_000) {
        lastOcclusionDiagnosticRef.current = now
        recordDocumentDiagnostic('document.capture.frame_deferred', 'info', { reason: 'transient_occlusion' })
      }
      return
    }
    occludedSinceRef.current = null
    lastOcclusionDiagnosticRef.current = null
    if (decision.state === 'no_page') {
      const guidance = '还没有看到完整清晰的作业页，请把题目放到高拍仪画面中央。'
      setMessage(guidance)
      speakGuidance('no_page', guidance)
      return
    }
    if (decision.state === 'writing') {
      setMessage('看到你还在移动或书写，我先等一下。')
      return
    }
    if (decision.state === 'stable') {
      // Freeze a full-size frame once, then assess and encode those same pixels.
      // The live video may advance between the low-resolution sample and here.
      const snapshot = document.createElement('canvas')
      snapshot.width = video.videoWidth
      snapshot.height = video.videoHeight
      try {
        const sourceContext = snapshot.getContext('2d')
        if (!sourceContext) throw new Error('CAPTURE_CONTEXT_UNAVAILABLE')
        sourceContext.drawImage(video, 0, 0, snapshot.width, snapshot.height)
        const sample = document.createElement('canvas')
        sample.width = 320
        sample.height = Math.max(180, Math.round(snapshot.height / snapshot.width * sample.width))
        const sampleContext = sample.getContext('2d', { willReadFrequently: true })
        if (!sampleContext || typeof sampleContext.getImageData !== 'function') throw new Error('FRAME_ANALYSIS_UNAVAILABLE')
        sampleContext.drawImage(snapshot, 0, 0, sample.width, sample.height)
        const snapshotFeatures = extractDocumentFrameFeatures(sampleContext.getImageData(0, 0, sample.width, sample.height))
        const current = deviceFrameClock.current.read('document_camera', video, streamRef.current, true, null)
        const currentAge = deviceFrameClock.current.frameAgeMs()
        if (!current.frame_confirmed || currentAge === null || currentAge >= 1500
          || !frameMonitorRef.current.matchesStableFrame(snapshotFeatures, Date.now(), deviceFrameClock.current.frameToken(), true)) {
          frameMonitorRef.current.invalidateStability()
          setMessage('页面刚刚发生变化，正在等待稳定后重新拍摄。')
          return
        }
      } catch {
        analysisUnavailable()
        return
      }
      setMessage('画面已经稳定，正在自动拍摄并在本机识别。')
      capture(snapshot)
      return
    }
    setMessage('正在等待题目画面稳定。')
  }

  const scheduleAutomatedCapture = (): void => {
    if (automation !== 'capture_and_recognize' || autoFrameIntervalRef.current !== null || captured) return
    inspectAutomatedFrame()
    autoFrameIntervalRef.current = window.setInterval(inspectAutomatedFrame, 450)
  }

  useEffect(() => {
    if (!open && !(parentPort && captured && leaseRef.current)) return
    const timer = window.setInterval(() => {
      const lease = leaseRef.current, generation = openGenerationRef.current
      if (lease) void (parentPort ? parentPort.renew(lease) : localApi.renewDeviceLease(lease)).catch((error: unknown) => {
        if (mountedRef.current && !privacyStoppedRef.current && leaseRef.current === lease && openGenerationRef.current === generation) {
          if (error instanceof ApiError && (error.status === 404 || error.status === 410)) {
            const recognitionPending = Boolean(ocrOwnerRef.current)
            release('document_capture_lease_expired')
            const message = recognitionPending
              ? '高拍仪连接已失效，这次识别结果尚未确认；已拍页面保留，请先核对结果。'
              : '高拍仪连接已失效，已经拍下的页面会保留。需要重新拍时，请再说“重新拍题”。'
            setMessage(message)
            onGuidance?.(message)
            if (!recognitionPending) report('failed', 'DEVICE_CAPTURE_UNAVAILABLE')
          } else {
            setMessage('设备租约续期失败；请关闭后重新打开高拍仪。')
          }
        }
      })
    }, 60_000)
    return () => window.clearInterval(timer)
  }, [open, captured, parentPort])

  const recognize = async (): Promise<void> => {
    if (!captured || privacyStoppedRef.current || !mountedRef.current || ocrOwnerRef.current) return
    const source = captureSourceRef.current
    if (parentPort && (source?.kind === 'file' ? !parentPort.recognizeFile
      : source?.kind !== 'camera' || !source.leaseId || leaseRef.current !== source.leaseId)) {
      setMessage('图片文件尚未取得家长导入授权，不能提交识别。原图片仍在本页；可以改用高拍仪重新拍摄。')
      return
    }
    const generation = openGenerationRef.current
    const version = captureVersionRef.current, owner = Symbol('document-ocr')
    const parentLease = parentPort && source?.kind === 'camera' ? source.leaseId : null
    let parentArtifactDelivered = false
    ocrOwnerRef.current = owner
    const isCurrent = (): boolean => generation === openGenerationRef.current && version === captureVersionRef.current
      && ocrOwnerRef.current === owner && !privacyStoppedRef.current && mountedRef.current
    const startedAt = Date.now()
    recordDocumentDiagnostic('document.capture.ocr_started')
    const pendingTimer = window.setTimeout(() => {
      if (isCurrent()) recordDocumentDiagnostic('document.capture.ocr_pending', 'warn', { latency_ms: Date.now() - startedAt })
    }, 90_000)
    setBusy(true); setMessage(null)
    try {
      const processed = parentPort ? captured : await processCapture(captured, rotation, crop)
      if (!isCurrent()) return
      let result: OcrArtifact
      if (parentPort && source?.kind === 'file') {
        if (fileRequestRef.current?.version !== version) fileRequestRef.current = { version, id: `parent-file-${crypto.randomUUID()}` }
        result = await parentPort.recognizeFile!(processed.split(',', 2)[1], { rotation, crop }, fileRequestRef.current.id)
      } else result = await (parentPort ? parentPort.recognize(processed.split(',', 2)[1], parentLease!, { rotation, crop }) : localApi.runOcr(sessionId, processed.split(',', 2)[1]))
      if (!isCurrent()) return
      const textCandidate = selectOcrTextCandidate(result)
      const quality = result.details.quality
      const review = result.details.local_pipeline?.manual_review
      const autoOrientation = result.details.local_pipeline?.quality.transforms?.orientation_degrees ?? 0
      const framingIssues = new Set(quality?.issues ?? [])
      const blocks = result.details.local_pipeline?.question_blocks
      const noCompleteQuestion = Boolean(blocks?.length && !blocks.some(block => !block.needs_manual_boundary_check))
      const readableManuscript = quality?.passed === true && Boolean(blocks?.length && blocks.every(block =>
        block.block_kind === 'fragment' && block.boundary_review_reasons?.length === 1
        && block.boundary_review_reasons[0] === 'missing_question_start'))
      const needsFramingRetry = automation === 'capture_and_recognize'
        && !textCandidate.text
        && source?.kind !== 'file'
        && (framingIssues.has('possible_hand_occlusion') || (noCompleteQuestion && !readableManuscript) || (
          !result.details.local_pipeline?.wrong_question_candidates?.length
          && ['content_near_edge', 'page_not_detected', 'blurred'].some((issue) => framingIssues.has(issue))
        ))
        && framingRetryCountRef.current < 3
      if (needsFramingRetry) {
        framingRetryCountRef.current += 1
        const transientOcclusion = framingIssues.has('possible_hand_occlusion')
        const guidance = framingIssues.has('possible_hand_occlusion')
          ? '画面暂时遮挡，正在持续观察，清晰后会自动识别。'
          : framingIssues.has('content_near_edge')
          ? '题目贴到画面边缘了，可能没有拍全。请把整道题移到画面中央，我会重新拍。'
          : framingIssues.has('blurred')
          ? '画面有些模糊，请保持纸张和高拍仪不动，我会重新拍。'
          : '还没有看清完整题目，请把题目放到画面中央，我会重新拍。'
        const retryVersion = reviseCapture()
        setCaptured(null)
        autoRecognizedCaptureRef.current = null
        setMessage(guidance)
        if (!transientOcclusion) speakGuidance('framing', guidance)
        recordDocumentDiagnostic('document.capture.retry_requested', transientOcclusion ? 'info' : 'warn', {
          reason: framingIssues.has('possible_hand_occlusion') ? 'occluded' : noCompleteQuestion ? 'question_boundary' : [...framingIssues][0] ?? 'framing',
          retry_count: framingRetryCountRef.current,
        })
        autoOpenTimerRef.current = window.setTimeout(() => {
          autoOpenTimerRef.current = null
          if (captureVersionRef.current === retryVersion && openGenerationRef.current === generation) void openCamera()
        }, 800)
        return
      }
      framingRetryCountRef.current = 0
      if (autoOrientation) setRotation((value) => (value + autoOrientation) % 360)
      report(textCandidate.text ? 'recognized' : 'failed', textCandidate.text ? undefined : 'OCR_EMPTY_TEXT', result.artifact_id, textCandidate.text ? 'completed' : 'failed')
      onArtifact?.(result)
      parentArtifactDelivered = Boolean(parentPort && onArtifact)
      recordDocumentDiagnostic('document.capture.ocr_completed', 'info', {
        latency_ms: Date.now() - startedAt,
        confidence: result.details.ocr_confidence ?? result.confidence,
        extracted_chars: textCandidate.text.length,
        review_candidate_source: textCandidate.source,
        quality_passed: Boolean(quality?.passed),
        wrong_question_candidates: result.details.local_pipeline?.wrong_question_candidates?.length ?? 0,
        auto_orientation: autoOrientation,
      })
      if (!textCandidate.text) {
        setMessage(quality?.messages.join(' ') || '没有识别到清晰文字，请重拍或手工输入。')
      } else {
        onText?.(textCandidate.text, result)
        const orientationNotice = autoOrientation ? `已自动转正${autoOrientation}度。` : ''
        setMessage(orientationNotice + (textCandidate.source === 'cloud_page'
          ? '本地识别不够可靠，已取得云端页内文字候选；请逐字确认后再讲解或加入错题本。'
          : review?.required
          ? reviewNotice(review)
          : quality?.passed
          ? `本地识别完成，OCR置信度${Math.round((result.details.ocr_confidence ?? result.confidence) * 100)}%。请确认文字后再加入作业。`
          : `${quality?.messages.join(' ')} 本次结果已按低置信处理，请重拍或逐字确认。`))
      }
    } catch (cause) {
      if (!isCurrent()) return
      report('failed', cause instanceof ApiError ? cause.code : 'OCR_FAILED', undefined, 'failed')
      recordDocumentDiagnostic('document.capture.ocr_failed', 'error', {
        latency_ms: Date.now() - startedAt,
        code: cause instanceof ApiError ? cause.code : 'OCR_FAILED',
      })
      if (cause instanceof CaptureTooLargeError) {
        setMessage(cause.message)
      } else if (parentPort && isCpuBusyCapture(cause)) {
        setMessage('本机 CPU 正忙，原页已保留。可以在待处理页面中稍后重试，无需重新拍摄。')
      } else {
        setMessage(cause instanceof ApiError ? cause.message : '图片识别失败，原表单内容没有丢失。')
      }
    } finally {
      window.clearTimeout(pendingTimer)
      if (ocrOwnerRef.current === owner) {
        ocrOwnerRef.current = null
        if (parentLease && leaseRef.current === parentLease) release('parent_ocr_settled')
        if (mountedRef.current) {
          if (parentArtifactDelivered && version === captureVersionRef.current) setCaptured(null)
          setBusy(false)
        }
      }
    }
  }

  useEffect(() => {
    if (automation !== 'capture_and_recognize' || !captured || busy || ocrOwnerRef.current
      || autoRecognizedCaptureRef.current === captureVersion) return
    autoRecognizedCaptureRef.current = captureVersion
    void recognize()
  }, [automation, captured, captureVersion, busy])

  const importFile = async (file: File | undefined): Promise<void> => {
    if (!file || privacyStoppedRef.current || !mountedRef.current) return
    if (!file.type.startsWith('image/') || file.size > 16_000_000) {
      setMessage('请选择不超过16MB的图片。')
      return
    }
    const version = reviseCapture()
    captureSourceRef.current = null
    release('capture_replaced')
    setCaptured(null); setRotation(0); setCrop(EMPTY_CROP); setMessage('正在载入图片。')
    try {
      const source = await fileToDataUrl(file)
      if (captureVersionRef.current !== version || privacyStoppedRef.current || !mountedRef.current) return
      captureSourceRef.current = { kind: 'file' }
      setCaptured(source)
      setMessage(parentPort && !parentPort.recognizeFile
        ? '图片文件尚未取得家长导入授权，不能提交识别。原图片仍在本页；可以改用高拍仪重新拍摄。'
        : '图片已载入，请检查裁切和方向。')
    } catch {
      if (captureVersionRef.current === version && !privacyStoppedRef.current && mountedRef.current) setMessage('图片未能载入，请重新选择。')
    }
  }

  const suggestCrop = async (): Promise<void> => {
    if (!captured || privacyStoppedRef.current) return
    const version = captureVersionRef.current
    try {
      const suggested = await suggestPageCrop(captured)
      if (version !== captureVersionRef.current || privacyStoppedRef.current || !mountedRef.current) return
      reviseCapture(); setCrop(suggested)
    } catch {
      if (version === captureVersionRef.current && !privacyStoppedRef.current && mountedRef.current) setMessage('未能取得裁切建议，原图片仍然保留。')
    }
  }

  return (
    <section className="document-capture" aria-label="高拍仪采集">
      <div className="capture-toolbar">
        {!open && !captured && <button className="secondary-button" disabled={opening} onClick={() => void openCamera()}><Camera size={17} />{opening ? '正在打开' : '打开高拍仪'}</button>}
        {!open && !captured && message && <button className="secondary-button" disabled={opening} onClick={() => void openCamera()}><RotateCw size={17} />重新查找高拍仪</button>}
        <label className="secondary-button file-input-button"><FileScan size={17} />导入图片<input type="file" accept="image/*" onChange={(event) => void importFile(event.target.files?.[0])} /></label>
        {deviceLabel && <span className="bound-device-label">{deviceLabel}</span>}
        {open && <><button className="primary-button" onClick={() => capture()}><ScanText size={17} />拍摄</button><button className="icon-button" title="关闭高拍仪" onClick={() => release('child_closed')}><X size={18} /></button></>}
      </div>
      {open && <video className="document-preview" ref={(element) => { videoRef.current = element }} onCanPlay={() => {
        const video = videoRef.current
        if (!parentPort && leaseRef.current && confirmedFrameLeaseRef.current !== leaseRef.current
          && streamRef.current?.getVideoTracks().some(track => track.readyState === 'live')
          && video?.srcObject === streamRef.current && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA
          && video.videoWidth > 0 && video.videoHeight > 0) {
          const lease = leaseRef.current
          confirmedFrameLeaseRef.current = lease
          void localApi.confirmDeviceFrame(lease, video.videoWidth, video.videoHeight).catch(() => {
            if (confirmedFrameLeaseRef.current === lease) confirmedFrameLeaseRef.current = null
          })
        }
        scheduleAutomatedCapture()
      }} muted playsInline />}
      {captured && <div className="capture-review">
        <div className="captured-frame"><img src={captured} alt="待识别的作业图片" style={{ transform: `rotate(${rotation}deg)`, clipPath: `inset(${crop.top}% ${crop.right}% ${crop.bottom}% ${crop.left}%)` }} /></div>
        <div className="capture-edit-tools">
          <button className="icon-button" title="顺时针旋转90度" onClick={() => { reviseCapture(); setRotation((value) => (value + 90) % 360) }}><RotateCw size={18} /></button>
          <button className="secondary-button" onClick={() => void suggestCrop()}><Crop size={17} />建议裁切</button>
          <button className="secondary-button" onClick={() => { reviseCapture(); setCrop(EMPTY_CROP) }}>恢复完整画面</button>
          <button className="secondary-button" onClick={() => { setCaptured(null); void openCamera() }}><Camera size={17} />重拍</button>
          <button className="primary-button" disabled={busy || Boolean(parentPort && (captureSourceRef.current?.kind === 'file'
            ? !parentPort.recognizeFile : !leaseRef.current))} onClick={() => void recognize()}><ScanText size={17} />{busy ? '本地识别中' : '确认并识别'}</button>
        </div>
        <div className="crop-controls">
          {(['top', 'right', 'bottom', 'left'] as const).map((side) => <label key={side}>{({ top: '上', right: '右', bottom: '下', left: '左' })[side]}边<input type="range" min="0" max="25" value={crop[side]} onChange={(event) => { reviseCapture(); setCrop((current) => ({ ...current, [side]: Number(event.target.value) })) }} /></label>)}
        </div>
      </div>}
      {message && <p className="capture-message">{message}</p>}
    </section>
  )
}

function reviewNotice(review: NonNullable<OcrArtifact['details']['local_pipeline']>['manual_review']): string {
  const areas = [review.handwriting === 'review_required' ? '手写内容' : '', review.math === 'review_required' ? '数学公式' : ''].filter(Boolean)
  return `${areas.join('和') || '页面结构'}需要人工确认；系统不会自动判定对错。请核对识别文字和题目边界后再加入作业。`
}

function isCpuBusyCapture(cause: unknown): boolean {
  if (!cause || typeof cause !== 'object') return false
  return (cause as { code?: unknown }).code === 'TEXTBOOK_OCR_CAPACITY_UNAVAILABLE'
}

function semanticParentCaptureMessage(cause: unknown): string | null {
  if (!cause || typeof cause !== 'object') return null
  const error = cause as { status?: unknown; message?: unknown }
  return (error.status === 409 || error.status === 404) && typeof error.message === 'string'
    ? error.message
    : null
}

async function processCapture(source: string, rotation: number, crop: CropMargins): Promise<string> {
  const image = await loadImage(source)
  const sourceX = image.width * crop.left / 100
  const sourceY = image.height * crop.top / 100
  const sourceWidth = image.width * (1 - (crop.left + crop.right) / 100)
  const sourceHeight = image.height * (1 - (crop.top + crop.bottom) / 100)
  const swap = rotation === 90 || rotation === 270
  const canvas = document.createElement('canvas')
  const outputWidth = swap ? sourceHeight : sourceWidth
  const outputHeight = swap ? sourceWidth : sourceHeight
  const scale = Math.min(1, Math.sqrt(MAX_OCR_PIXELS / (outputWidth * outputHeight)))
  canvas.width = Math.max(1, Math.floor(outputWidth * scale))
  canvas.height = Math.max(1, Math.floor(outputHeight * scale))
  const context = canvas.getContext('2d')!
  context.translate(canvas.width / 2, canvas.height / 2)
  context.rotate(rotation * Math.PI / 180)
  const scaledWidth = swap ? canvas.height : canvas.width
  const scaledHeight = swap ? canvas.width : canvas.height
  context.drawImage(image, sourceX, sourceY, sourceWidth, sourceHeight, -scaledWidth / 2, -scaledHeight / 2, scaledWidth, scaledHeight)
  return encodeBoundedJpeg(canvas)
}

async function suggestPageCrop(source: string): Promise<CropMargins> {
  const image = await loadImage(source)
  const canvas = document.createElement('canvas')
  const scale = Math.min(1, 480 / image.width)
  canvas.width = Math.round(image.width * scale); canvas.height = Math.round(image.height * scale)
  const context = canvas.getContext('2d')!; context.drawImage(image, 0, 0, canvas.width, canvas.height)
  const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data
  let minX = canvas.width; let minY = canvas.height; let maxX = 0; let maxY = 0; let count = 0
  for (let y = 0; y < canvas.height; y += 2) for (let x = 0; x < canvas.width; x += 2) {
    const offset = (y * canvas.width + x) * 4
    const r = pixels[offset]; const g = pixels[offset + 1]; const b = pixels[offset + 2]
    if ((r + g + b) / 3 > 155 && Math.max(r, g, b) - Math.min(r, g, b) < 42) {
      minX = Math.min(minX, x); maxX = Math.max(maxX, x); minY = Math.min(minY, y); maxY = Math.max(maxY, y); count += 1
    }
  }
  if (count < canvas.width * canvas.height * 0.08 || maxX <= minX || maxY <= minY) return EMPTY_CROP
  const padding = 2
  return {
    left: Math.max(0, Math.round(minX / canvas.width * 100) - padding),
    right: Math.max(0, Math.round((canvas.width - maxX) / canvas.width * 100) - padding),
    top: Math.max(0, Math.round(minY / canvas.height * 100) - padding),
    bottom: Math.max(0, Math.round((canvas.height - maxY) / canvas.height * 100) - padding),
  }
}

function loadImage(source: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => { const image = new Image(); image.onload = () => resolve(image); image.onerror = reject; image.src = source })
}

function fileToDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = reject; reader.readAsDataURL(file) })
}
