import { mediaDeviceFailure, mediaDeviceRegistration, openObservationCameraStream, openDocumentCameraStream, resolveBoundVideoDevice } from './mediaDevices'
import type { DeviceBinding, DeviceQualificationMode, DeviceQualificationRun } from './types'

export type QualificationDeviceMetrics = {
  samples: number
  advancing_samples: number
  frozen_samples: number
  max_consecutive_frozen: number
  frame_count: number
  frame_callback_supported: boolean
  content_change_samples: number
  low_variation_samples: number
  ended: boolean
  width: number | null
  height: number | null
  frame_rate: number | null
}

export type QualificationMetrics = {
  elapsed_seconds: number
  expected_seconds: number
  sample_interval_ms: number
  devices: Partial<Record<'camera' | 'document_camera', QualificationDeviceMetrics>>
}

export type QualificationProgress = {
  elapsedSeconds: number
  expectedSeconds: number
  devices: QualificationMetrics['devices']
}

export type QualificationResult = {
  outcome: 'passed' | 'failed' | 'cancelled'
  metrics: QualificationMetrics
  failureCode?: string
}

const SAMPLE_INTERVAL_MS = 1000
let pendingNativeAcquisitions = 0

export async function runVideoQualification({
  mode,
  bindings,
  expectedSeconds,
  signal,
  onProgress,
}: {
  mode: DeviceQualificationMode
  bindings: DeviceBinding[]
  expectedSeconds: number
  signal: AbortSignal
  onProgress: (progress: QualificationProgress) => void
}): Promise<QualificationResult> {
  const roles = qualificationRoles(mode)
  const resources: Array<{
    role: 'camera' | 'document_camera'
    stream: MediaStream
    track: MediaStreamTrack
    video: HTMLVideoElement
    frameRequestId?: number
    metrics: QualificationDeviceMetrics
    previousTime: number
    previousFrameCount: number
    previousFingerprint: Uint8Array | null
    consecutiveFrozen: number
    fingerprintCanvas: HTMLCanvasElement
  }> = []
  let captureStartedAt: number | null = null
  const captureElapsedSeconds = (): number => captureStartedAt === null ? 0 : (performance.now() - captureStartedAt) / 1000
  const ownedStreams = new Set<MediaStream>()
  const nativeAcquisitions = new Set<Promise<MediaStream>>()
  let captureClosed = false
  const closeStream = (stream: MediaStream): void => {
    if (!ownedStreams.delete(stream)) return
    stream.getTracks().forEach((track) => track.stop())
  }
  const closeOwnedStreams = (): void => {
    captureClosed = true
    for (const stream of [...ownedStreams]) closeStream(stream)
    for (const resource of resources) {
      if (resource.frameRequestId !== undefined && typeof resource.video.cancelVideoFrameCallback === 'function') {
        resource.video.cancelVideoFrameCallback(resource.frameRequestId)
        resource.frameRequestId = undefined
      }
      resource.video.srcObject = null
    }
  }
  const acquire = (constraints: MediaStreamConstraints): Promise<MediaStream> => {
    if (signal.aborted || captureClosed) return Promise.reject(new DOMException('Qualification closed', 'QualificationClosedError'))
    pendingNativeAcquisitions += 1
    const pending = Promise.resolve().then(() => navigator.mediaDevices.getUserMedia(constraints)).then(stream => {
      ownedStreams.add(stream)
      if (signal.aborted || captureClosed) {
        closeStream(stream)
        throw new DOMException('Qualification closed', 'QualificationClosedError')
      }
      return stream
    }).finally(() => { pendingNativeAcquisitions -= 1; nativeAcquisitions.delete(pending) })
    nativeAcquisitions.add(pending)
    return pending
  }
  signal.addEventListener('abort', closeOwnedStreams, { once: true })
  try {
    if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
    if (pendingNativeAcquisitions) throw new QualificationError('DEVICE_ACQUISITION_PENDING')
    if (!navigator.mediaDevices?.getUserMedia) throw new QualificationError('MEDIA_API_UNAVAILABLE')
    const devices = await abortableStartup(() => navigator.mediaDevices.enumerateDevices(), signal)
    for (const role of roles) {
      const binding = bindings.find((item) => item.device_type === role)
      if (!binding) throw new QualificationError('DEVICE_NOT_BOUND')
      const matched = await abortableStartup(() => resolveBoundVideoDevice(devices, binding.system_id_hash, binding.label, role), signal)
      if (!matched) throw new QualificationError('BOUND_DEVICE_NOT_FOUND')
      if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
      // Qualify the production capture modes, including the selected L523
      // mode and scanner resolution ladder. An unconstrained 30fps test is
      // not evidence for the modes used during learning.
      const stream = role === 'camera'
        ? await openObservationCameraStream(matched.device.deviceId, matched.device.label || binding.label, acquire, binding.system_id_hash)
        : await openDocumentCameraStream(matched.device.deviceId, 'capture', acquire, binding.system_id_hash)
      const track = stream.getVideoTracks()[0]
      if (!track) {
        closeStream(stream)
        throw new QualificationError('VIDEO_TRACK_MISSING')
      }
      const registration = await abortableStartup(() => mediaDeviceRegistration(track), signal)
      if (matched.method === 'system_id_hash' && registration.systemIdHash !== binding.system_id_hash) {
        closeStream(stream)
        throw new QualificationError('DEVICE_IDENTITY_MISMATCH')
      }
      const settings = track.getSettings()
      const video = document.createElement('video')
      video.muted = true
      video.autoplay = true
      video.playsInline = true
      video.setAttribute('aria-hidden', 'true')
      Object.assign(video.style, { position: 'fixed', width: '2px', height: '2px', opacity: '0', pointerEvents: 'none', left: '-10px', top: '-10px' })
      video.srcObject = stream
      document.body.append(video)
      const metrics: QualificationDeviceMetrics = {
        samples: 0,
        advancing_samples: 0,
        frozen_samples: 0,
        max_consecutive_frozen: 0,
        frame_count: 0,
        frame_callback_supported: typeof video.requestVideoFrameCallback === 'function',
        content_change_samples: 0,
        low_variation_samples: 0,
        ended: false,
        width: settings.width ?? null,
        height: settings.height ?? null,
        frame_rate: settings.frameRate ?? null,
      }
      track.addEventListener('ended', () => { metrics.ended = true }, { once: true })
      const fingerprintCanvas = document.createElement('canvas')
      fingerprintCanvas.width = 32
      fingerprintCanvas.height = 18
      const resource = {
        role, stream, track, video, metrics, previousTime: 0, previousFrameCount: 0,
        previousFingerprint: null, consecutiveFrozen: 0, fingerprintCanvas,
      } as typeof resources[number]
      if (metrics.frame_callback_supported) {
        const countFrame: VideoFrameRequestCallback = () => {
          metrics.frame_count += 1
          if (!signal.aborted && !metrics.ended && track.readyState === 'live') resource.frameRequestId = video.requestVideoFrameCallback(countFrame)
        }
        resource.frameRequestId = video.requestVideoFrameCallback(countFrame)
      }
      resources.push(resource)
      await abortableStartup(() => video.play(), signal)
      resource.previousTime = video.currentTime
      resource.previousFrameCount = metrics.frame_count
      updateMediaEvidence(resource, 0)
    }

    // Startup is not capture evidence. Both streams must be playing before
    // the shared observation window and its frame counters begin.
    captureStartedAt = performance.now()
    for (const resource of resources) {
      resource.metrics.frame_count = 0
      resource.previousFrameCount = 0
      resource.previousTime = resource.video.currentTime
    }
    let nextSampleAt = captureStartedAt + SAMPLE_INTERVAL_MS
    while (captureElapsedSeconds() < expectedSeconds) {
      await abortableDelay(Math.max(0, nextSampleAt - performance.now()), signal)
      for (const resource of resources) {
        resource.metrics.samples += 1
        if (resource.track.readyState !== 'live') resource.metrics.ended = true
        const mediaTimeAdvanced = resource.video.currentTime > resource.previousTime + 0.01
        const callbackAdvanced = !resource.metrics.frame_callback_supported || resource.metrics.frame_count > resource.previousFrameCount
        if (mediaTimeAdvanced && callbackAdvanced) {
          resource.metrics.advancing_samples += 1
          resource.consecutiveFrozen = 0
        } else {
          resource.metrics.frozen_samples += 1
          resource.consecutiveFrozen += 1
          resource.metrics.max_consecutive_frozen = Math.max(resource.metrics.max_consecutive_frozen, resource.consecutiveFrozen)
        }
        const fingerprint = frameFingerprint(resource.video, resource.fingerprintCanvas)
        if (fingerprint && resource.previousFingerprint) {
          if (meanAbsoluteDifference(fingerprint, resource.previousFingerprint) >= 0.8) resource.metrics.content_change_samples += 1
          else resource.metrics.low_variation_samples += 1
        }
        resource.previousFingerprint = fingerprint
        resource.previousTime = resource.video.currentTime
        resource.previousFrameCount = resource.metrics.frame_count
        updateMediaEvidence(resource, captureElapsedSeconds())
      }
      const elapsedSeconds = Math.min(expectedSeconds, Math.floor(captureElapsedSeconds()))
      onProgress({ elapsedSeconds, expectedSeconds, devices: metricsSnapshot(resources) })
      if (resources.some((resource) => resource.metrics.ended)) break
      // Processing time must not accumulate between samples. Missed periods
      // remain missing evidence; never backfill them with synthetic samples.
      nextSampleAt += SAMPLE_INTERVAL_MS
      if (nextSampleAt <= performance.now()) {
        nextSampleAt = captureStartedAt + (Math.floor(captureElapsedSeconds()) + 1) * SAMPLE_INTERVAL_MS
      }
    }
    const metrics: QualificationMetrics = {
      elapsed_seconds: Math.round(captureElapsedSeconds()),
      expected_seconds: expectedSeconds,
      sample_interval_ms: SAMPLE_INTERVAL_MS,
      devices: metricsSnapshot(resources),
    }
    const failureCode = qualificationFailure(metrics)
    return { outcome: failureCode ? 'failed' : 'passed', metrics, failureCode: failureCode ?? undefined }
  } catch (cause) {
    const metrics: QualificationMetrics = {
      elapsed_seconds: Math.round(captureElapsedSeconds()),
      expected_seconds: expectedSeconds,
      sample_interval_ms: SAMPLE_INTERVAL_MS,
      devices: metricsSnapshot(resources),
    }
    if (signal.aborted || (cause instanceof DOMException && cause.name === 'AbortError')) {
      return { outcome: 'cancelled', metrics, failureCode: 'USER_CANCELLED' }
    }
    const failureCode = cause instanceof QualificationError
      ? cause.code
      : mediaDeviceFailure(cause, '视频设备').code
    return { outcome: 'failed', metrics, failureCode }
  } finally {
    signal.removeEventListener('abort', closeOwnedStreams)
    closeOwnedStreams()
    // A shared opener's deadline cannot cancel its original native request.
    // Retain it and close any late stream before reporting final cleanup.
    await Promise.allSettled([...nativeAcquisitions])
    for (const resource of resources) {
      if (resource.frameRequestId !== undefined && typeof resource.video.cancelVideoFrameCallback === 'function') {
        resource.video.cancelVideoFrameCallback(resource.frameRequestId)
      }
      resource.video.srcObject = null
      resource.video.remove()
    }
  }
}

function abortableStartup<T>(work: () => Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(new DOMException('Aborted', 'AbortError'))
  return new Promise((resolve, reject) => {
    let settled = false
    const finish = (value: T | undefined, failed: boolean, error?: unknown): void => {
      if (settled) return
      settled = true
      signal.removeEventListener('abort', aborted)
      if (failed) reject(error)
      else resolve(value as T)
    }
    const aborted = (): void => finish(undefined, true, new DOMException('Aborted', 'AbortError'))
    signal.addEventListener('abort', aborted, { once: true })
    void Promise.resolve().then(() => {
      if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
      return work()
    }).then((value) => finish(value, false), (error) => finish(undefined, true, error))
  })
}

export function qualificationFailure(metrics: QualificationMetrics): string | null {
  if (Object.values(metrics.devices).some(device => device?.ended)) return 'DEVICE_DISCONNECTED'
  if (metrics.elapsed_seconds + 1 < metrics.expected_seconds) return 'TEST_INCOMPLETE'
  for (const device of Object.values(metrics.devices)) {
    if (!device) continue
    if (device.ended) return 'DEVICE_DISCONNECTED'
    if (device.samples === 0) return 'NO_FRAME_SAMPLES'
    if (device.samples < Math.max(1, Math.floor(metrics.expected_seconds * 0.98))) return 'INSUFFICIENT_FRAME_SAMPLES'
    if (
      !Number.isInteger(device.advancing_samples) || !Number.isInteger(device.frozen_samples)
      || device.advancing_samples < 0 || device.frozen_samples < 0
      || device.advancing_samples + device.frozen_samples !== device.samples
    ) return 'INVALID_FRAME_METRICS'
    const allowedFrozen = Math.max(3, Math.ceil(device.samples * 0.005))
    if (device.max_consecutive_frozen > 3 || device.frozen_samples > allowedFrozen) return 'FRAME_STALLED'
    if (device.frame_callback_supported && device.frame_count < device.advancing_samples) return 'INVALID_FRAME_METRICS'
    if (!positiveNumber(device.width) || !positiveNumber(device.height) || !positiveNumber(device.frame_rate)) return 'DEVICE_CAPABILITIES_INCOMPLETE'
  }
  return null
}

export function qualificationRoles(mode: DeviceQualificationMode): Array<'camera' | 'document_camera'> {
  if (mode === 'camera_stability') return ['camera']
  if (mode === 'document_stability') return ['document_camera']
  return ['camera', 'document_camera']
}

export function qualificationEvidenceLabel(run: DeviceQualificationRun): string {
  const metrics = run.metrics as {
    devices?: Record<string, Record<string, unknown>>
    validation?: { server_elapsed_seconds?: unknown }
  }
  const roleLabels: Record<string, string> = { camera: '观察摄像头', document_camera: '高拍仪' }
  const deviceEvidence = Object.entries(metrics.devices ?? {}).map(([role, item]) => {
    const width = readableNumber(item.width)
    const height = readableNumber(item.height)
    const frameRate = readableNumber(item.frame_rate, 1)
    const samples = readableNumber(item.samples)
    const frozen = readableNumber(item.frozen_samples)
    const longest = readableNumber(item.max_consecutive_frozen)
    const changed = readableNumber(item.content_change_samples)
    const lowVariation = readableNumber(item.low_variation_samples)
    return `${roleLabels[role] ?? role} ${width}×${height} · ${frameRate}fps · 采样${samples}/冻结${frozen}（最长${longest}秒） · 变化${changed}/低变化${lowVariation}`
  })
  const serverElapsed = readableNumber(metrics.validation?.server_elapsed_seconds, 1)
  return deviceEvidence.length > 0
    ? `${deviceEvidence.join(' ； ')} · 服务端复核${serverElapsed}秒`
    : run.failure_code ? qualificationFailureLabel(run.failure_code) : '本次没有完整视频证据'
}

function qualificationFailureLabel(code?: string): string {
  return ({ DEVICE_DISCONNECTED: '设备连接中断', FRAME_STALLED: '画面长时间未推进', TEST_INCOMPLETE: '测试未达到规定时长', NO_FRAME_SAMPLES: '没有获得画面采样', INSUFFICIENT_FRAME_SAMPLES: '有效采样未达到规定时长的98%', INVALID_FRAME_METRICS: '画面计数不一致', DEVICE_CAPABILITIES_INCOMPLETE: '缺少实际分辨率或FPS证据' } as Record<string, string>)[code ?? ''] ?? '本次没有完整视频证据'
}

function readableNumber(value: unknown, fractionDigits = 0): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(fractionDigits) : '未记录'
}

function metricsSnapshot(resources: Array<{ role: 'camera' | 'document_camera'; metrics: QualificationDeviceMetrics }>): QualificationMetrics['devices'] {
  return Object.fromEntries(resources.map((resource) => [resource.role, { ...resource.metrics }]))
}

function updateMediaEvidence(resource: {
  video: HTMLVideoElement
  track: MediaStreamTrack
  metrics: QualificationDeviceMetrics
}, elapsedSeconds: number): void {
  const settings = resource.track.getSettings()
  resource.metrics.width = settings.width ?? (resource.video.videoWidth || resource.metrics.width)
  resource.metrics.height = settings.height ?? (resource.video.videoHeight || resource.metrics.height)
  resource.metrics.frame_rate = settings.frameRate
    ?? (resource.metrics.frame_callback_supported && elapsedSeconds > 0 ? Number((resource.metrics.frame_count / elapsedSeconds).toFixed(2)) : resource.metrics.frame_rate)
}

function frameFingerprint(video: HTMLVideoElement, canvas: HTMLCanvasElement): Uint8Array | null {
  if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA || video.videoWidth <= 0 || video.videoHeight <= 0) return null
  const context = canvas.getContext('2d', { willReadFrequently: true })
  if (!context) return null
  context.drawImage(video, 0, 0, canvas.width, canvas.height)
  const rgba = context.getImageData(0, 0, canvas.width, canvas.height).data
  const gray = new Uint8Array(canvas.width * canvas.height)
  for (let source = 0, target = 0; source < rgba.length; source += 4, target += 1) {
    gray[target] = Math.round((rgba[source] + rgba[source + 1] + rgba[source + 2]) / 3)
  }
  return gray
}

function meanAbsoluteDifference(left: Uint8Array, right: Uint8Array): number {
  if (left.length !== right.length || left.length === 0) return Number.POSITIVE_INFINITY
  let total = 0
  for (let index = 0; index < left.length; index += 1) total += Math.abs(left[index] - right[index])
  return total / left.length
}

function positiveNumber(value: number | null): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
}

function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(new DOMException('Aborted', 'AbortError'))
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(done, milliseconds)
    signal.addEventListener('abort', aborted, { once: true })
    function done(): void {
      signal.removeEventListener('abort', aborted)
      resolve()
    }
    function aborted(): void {
      window.clearTimeout(timeout)
      reject(new DOMException('Aborted', 'AbortError'))
    }
  })
}

class QualificationError extends Error {
  constructor(readonly code: string) {
    super(code)
  }
}
