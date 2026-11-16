export async function mediaDeviceRegistration(track: MediaStreamTrack): Promise<{ systemIdHash: string; capabilities: Record<string, unknown> }> {
  const settings = track.getSettings()
  const supported = typeof track.getCapabilities === 'function'
    ? track.getCapabilities() as MediaTrackCapabilities & Record<string, unknown>
    : {} as MediaTrackCapabilities & Record<string, unknown>
  const enumerated = await navigator.mediaDevices?.enumerateDevices().catch(() => [] as MediaDeviceInfo[]) ?? []
  const sameGroupAudio = Boolean(settings.groupId) && enumerated.some((device) =>
    device.kind === 'audioinput' && device.groupId === settings.groupId
  )
  const ptzAxes = ['pan', 'tilt', 'zoom'].filter((axis) => supported[axis] !== undefined)
  const systemIdHash = await mediaDeviceIdentityHash(settings.deviceId ?? '', settings.groupId ?? '')
  return {
    systemIdHash,
    capabilities: {
      system_enumeration_name: track.label || '未命名媒体设备',
      identity_version: 'browser_device_id_v2',
      width: settings.width ?? null,
      height: settings.height ?? null,
      frame_rate: settings.frameRate ?? null,
      supported_width: numericRange(supported.width),
      supported_height: numericRange(supported.height),
      supported_frame_rate: numericRange(supported.frameRate),
      supported_focus_modes: stringList(supported.focusMode),
      focus_mode: typeof (settings as MediaTrackSettings & Record<string, unknown>).focusMode === 'string'
        ? (settings as MediaTrackSettings & Record<string, unknown>).focusMode
        : null,
      transport: 'browser-media-device',
      browser_ptz_axes: ptzAxes,
      ptz_control: ptzAxes.length > 0 ? 'browser_constraints_detected_unverified' : 'not_exposed_by_browser',
      audio_input_same_group: sameGroupAudio,
    }
  }
}

export type DocumentCameraAutofocus = {
  exposed: boolean
  applied: boolean
  requested_mode: 'continuous' | 'single-shot' | null
  active_mode: string | null
  error_code?: string
}

/** Prefer continuous autofocus and fall back to one hardware focus cycle. */
export async function configureDocumentCameraAutofocus(track: MediaStreamTrack): Promise<DocumentCameraAutofocus> {
  const capabilities = typeof track.getCapabilities === 'function'
    ? track.getCapabilities() as MediaTrackCapabilities & Record<string, unknown>
    : {} as MediaTrackCapabilities & Record<string, unknown>
  const modes = stringList(capabilities.focusMode)
  const requestedMode = modes.includes('continuous') ? 'continuous' : modes.includes('single-shot') ? 'single-shot' : null
  const currentMode = (track.getSettings() as MediaTrackSettings & Record<string, unknown>).focusMode
  if (!requestedMode || typeof track.applyConstraints !== 'function') {
    return { exposed: modes.length > 0, applied: false, requested_mode: null, active_mode: typeof currentMode === 'string' ? currentMode : null }
  }
  try {
    await track.applyConstraints({ advanced: [{ focusMode: requestedMode }] } as unknown as MediaTrackConstraints)
    const activeMode = (track.getSettings() as MediaTrackSettings & Record<string, unknown>).focusMode
    return { exposed: true, applied: true, requested_mode: requestedMode, active_mode: typeof activeMode === 'string' ? activeMode : requestedMode }
  } catch (cause) {
    return {
      exposed: true, applied: false, requested_mode: requestedMode,
      active_mode: typeof currentMode === 'string' ? currentMode : null,
      error_code: cause instanceof Error ? cause.name || 'AUTOFOCUS_CONSTRAINT_FAILED' : 'AUTOFOCUS_CONSTRAINT_FAILED'
    }
  }
}

export async function mediaDeviceInfoHash(device: Pick<MediaDeviceInfo, 'deviceId' | 'groupId' | 'label'>): Promise<string> {
  return mediaDeviceIdentityHash(device.deviceId, device.groupId)
}

export async function findBoundVideoDevice(
  devices: Array<Pick<MediaDeviceInfo, 'kind' | 'deviceId' | 'groupId' | 'label'>>,
  systemIdHash: string,
  boundLabel?: string,
  role?: VideoDeviceRole,
): Promise<Pick<MediaDeviceInfo, 'kind' | 'deviceId' | 'groupId' | 'label'> | null> {
  return (await resolveBoundVideoDevice(devices, systemIdHash, boundLabel, role))?.device ?? null
}

export type VideoDeviceRole = 'camera' | 'document_camera'

function isIntegratedVideoDevice(device: Pick<MediaDeviceInfo, 'label'>): boolean {
  const label = normalizedDeviceName(device.label)
  return /(^| )(integrated|built in|builtin|internal)( |$)/.test(label) || /(^| )(集成|内置)( |$)/.test(label)
}

export function isExternalVideoDevice(device: Pick<MediaDeviceInfo, 'label'>): boolean {
  return !isIntegratedVideoDevice(device)
}

function roleScore(device: Pick<MediaDeviceInfo, 'label'>, role: VideoDeviceRole): number {
  if (isIntegratedVideoDevice(device)) return Number.NEGATIVE_INFINITY
  const label = normalizedDeviceName(device.label)
  if (role === 'camera') {
    if (/(^| )l523( |$)/.test(label)) return 100
    if (/(tracking|tracker|ptz|webcam|观察|云台)/.test(label)) return 50
    return 0
  }
  if (/(^| )usb hdcamera( |$)/.test(label)) return 100
  if (/(document|visualizer|scanner|desk|高拍|展台)/.test(label)) return 50
  return 0
}

export function isNamedDocumentCamera(device: Pick<MediaDeviceInfo, 'label'>): boolean {
  return roleScore(device, 'document_camera') > 0
}

/**
 * Selects the known external observation camera and document camera without
 * ever falling back to a laptop's integrated RGB/IR cameras. Unknown external
 * devices are left unassigned instead of being guessed by enumeration order.
 */
export function recommendVideoRoleDevices(
  devices: Array<Pick<MediaDeviceInfo, 'kind' | 'deviceId' | 'groupId' | 'label'>>,
): { cameraId: string; documentCameraId: string } {
  const videos = devices.filter((device) => device.kind === 'videoinput' && !isIntegratedVideoDevice(device))
  const choose = (role: VideoDeviceRole, excluded = '') => videos
    .filter((device) => device.deviceId !== excluded)
    .map((device) => ({ device, score: roleScore(device, role) }))
    .filter((candidate) => candidate.score > 0)
    .sort((left, right) => right.score - left.score)[0]?.device.deviceId ?? ''
  const cameraId = choose('camera')
  return { cameraId, documentCameraId: choose('document_camera', cameraId) }
}

export type BoundVideoDeviceMatch = {
  device: Pick<MediaDeviceInfo, 'kind' | 'deviceId' | 'groupId' | 'label'>
  method: 'system_id_hash' | 'unique_exact_label'
}

export type DocumentCameraResolution = Readonly<{ width: number; height: number }>

/**
 * The deployed Usb HDCamera was proven on Windows to deliver 4160x3120 at
 * 5fps while the 1280x720 observation camera remained live. Exact modes stop
 * Chromium from silently handing OCR a 720p stream; lower modes are attempted
 * only when the hardware rejects the higher exact mode.
 */
export const DOCUMENT_CAMERA_RESOLUTION_LADDER: readonly DocumentCameraResolution[] = [
  { width: 4160, height: 3120 },
  { width: 3840, height: 2160 },
  { width: 3264, height: 2448 },
  { width: 2560, height: 1440 },
  { width: 1920, height: 1080 },
  { width: 1280, height: 720 },
]

export function documentCameraVideoConstraints(
  deviceId: string,
  _mode: 'capture' | 'monitor' = 'capture',
  resolution: DocumentCameraResolution = DOCUMENT_CAMERA_RESOLUTION_LADDER[0],
): MediaTrackConstraints {
  const constraints: MediaTrackConstraints & { resizeMode: { ideal: string } } = {
    deviceId: { exact: deviceId },
    width: { exact: resolution.width },
    height: { exact: resolution.height },
    resizeMode: { ideal: 'none' },
    frameRate: { ideal: 5, max: 10 },
  }
  return constraints
}

type GetUserMedia = (constraints: MediaStreamConstraints) => Promise<MediaStream>

/**
 * Windows UVC drivers can transiently report "Timeout starting video source"
 * while the observation camera is opening. Retry only start/contention errors;
 * permission and missing-device failures remain immediate and actionable.
 */
export async function openDocumentCameraStream(
  deviceId: string,
  mode: 'capture' | 'monitor' = 'capture',
  acquire: GetUserMedia = (constraints) => navigator.mediaDevices.getUserMedia(constraints),
  bindingIdentity?: string,
): Promise<MediaStream> {
  return startBoundVideoStream(deviceId, async expired => {
    let lastError: unknown
    for (const resolution of DOCUMENT_CAMERA_RESOLUTION_LADDER) {
      const retryDelays = [0, 1_200, 1_800]
      for (const delay of retryDelays) {
        if (delay) await new Promise((resolve) => setTimeout(resolve, delay))
        // A deadline cannot cancel native getUserMedia. Do not turn its late
        // failure (or a delayed retry) into another physical acquisition.
        if (expired()) throw new DOMException('Camera start exceeded 20 seconds', 'CameraStartTimeoutError')
        try {
          return await acquire({ audio: false, video: documentCameraVideoConstraints(deviceId, mode, resolution) })
        } catch (cause) {
          lastError = cause
          if (expired()) throw cause
          const name = browserMediaErrorName(cause)
          if (name === 'OverconstrainedError' || name === 'ConstraintNotSatisfiedError') break
          if (!['AbortError', 'NotReadableError', 'TrackStartError'].includes(name)) throw cause
        }
      }
    }
    throw lastError
  }, bindingIdentity)
}

/** L523 and similar UVC cameras can advertise 640x360 but never deliver it. */
export function observationCameraVideoConstraints(deviceId: string, deviceLabel = ''): MediaTrackConstraints {
  // The deployed Windows L523 starts and delivers frames in this advertised
  // MJPEG mode, while its 720p capture path can fail to start. This exception
  // applies only to the selected, bound L523; other cameras keep their profile.
  if (/Windows/i.test(navigator.userAgent) && normalizedDeviceName(deviceLabel) === 'l523') {
    return {
      deviceId: { exact: deviceId },
      width: { exact: 1920 }, height: { exact: 1088 },
      frameRate: { ideal: 10, max: 10 }, resizeMode: { ideal: 'none' },
    } as MediaTrackConstraints
  }
  return {
    deviceId: { exact: deviceId },
    width: { ideal: 1280, max: 1280 },
    height: { ideal: 720, max: 720 },
    frameRate: { ideal: 10, max: 15 },
  }
}

const pendingVideoStarts = new Map<string, Promise<MediaStream>>()

/** getUserMedia cannot be aborted. Keep the bound device quarantined after a
 * deadline until the actual request settles, and stop any late stream. This
 * survives workspace replacement, so a new child cannot overlap the old open. */
export async function openObservationCameraStream(
  deviceId: string,
  deviceLabel = '',
  acquire: GetUserMedia = constraints => navigator.mediaDevices.getUserMedia(constraints),
  bindingIdentity?: string,
): Promise<MediaStream> {
  return startBoundVideoStream(deviceId, async () => acquire({ audio: false,
    video: observationCameraVideoConstraints(deviceId, deviceLabel) }), bindingIdentity)
}

/** One deadline covers a scanner's resolution/retry ladder as well as a
 * single observation request. Hold the same device across retry delays and
 * workspace/role changes until the actual operation has settled. */
function startBoundVideoStream(
  deviceId: string,
  acquire: (expired: () => boolean) => Promise<MediaStream>,
  bindingIdentity?: string,
): Promise<MediaStream> {
  // A reconnect can rotate Chromium's id while the native request is still
  // pending. The persisted binding joins old/new ids without guessing labels.
  const identities = [`device:${deviceId}`, ...(bindingIdentity ? [`binding:${bindingIdentity}`] : [])]
  if (identities.some(identity => pendingVideoStarts.has(identity))) {
    throw new DOMException('The previous camera start has not settled', 'CameraStartPendingError')
  }
  let expired = false
  const pending = Promise.resolve().then(() => acquire(() => expired))
  identities.forEach(identity => pendingVideoStarts.set(identity, pending))
  const settled = (): void => {
    identities.forEach(identity => {
      if (pendingVideoStarts.get(identity) === pending) pendingVideoStarts.delete(identity)
    })
  }
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      expired = true
      reject(new DOMException('Camera start exceeded 20 seconds', 'CameraStartTimeoutError'))
    }, 20_000)
    void pending.then(stream => {
      settled()
      clearTimeout(timer)
      if (expired) stream.getTracks().forEach(track => track.stop())
      else resolve(stream)
    }, cause => {
      settled()
      clearTimeout(timer)
      if (!expired) reject(cause)
    })
  })
}

/**
 * Resolves a persisted camera binding consistently for every opener.  A
 * browser-device-id hash remains the proof of identity.  A Chromium profile
 * reset may rotate that id, so one non-empty exact label is allowed only when
 * it is unique; same-named devices remain a closed failure requiring a parent
 * rebind.
 */
export async function resolveBoundVideoDevice(
  devices: Array<Pick<MediaDeviceInfo, 'kind' | 'deviceId' | 'groupId' | 'label'>>,
  systemIdHash: string,
  boundLabel?: string,
  role?: VideoDeviceRole,
): Promise<BoundVideoDeviceMatch | null> {
  const videos = devices.filter((device) => device.kind === 'videoinput' && (!role || !isIntegratedVideoDevice(device)))
  const hashes = await Promise.all(videos.map((device) => mediaDeviceInfoHash(device)))
  const index = hashes.indexOf(systemIdHash)
  if (index >= 0) return { device: videos[index], method: 'system_id_hash' }

  // Chromium may rotate deviceId values after an Electron profile reset.  A
  // persisted hash is still the primary proof of identity; this constrained
  // fallback only restores a previously verified device when its non-empty
  // label has one exact normalized match.  It deliberately never uses a
  // substring or an enumeration-order heuristic, so two same-named cameras
  // remain a closed failure requiring parent re-binding.
  const normalizedBoundLabel = normalizedDeviceName(boundLabel ?? '')
  if (!normalizedBoundLabel) return null
  const exactLabelMatches = videos.filter((device) => normalizedDeviceName(device.label) === normalizedBoundLabel)
  return exactLabelMatches.length === 1 ? { device: exactLabelMatches[0], method: 'unique_exact_label' } : null
}

/** Hash first; Chromium device-id rotation may use one exact non-empty label fallback only. */
export async function findBoundAudioDevice(
  devices: Array<Pick<MediaDeviceInfo, 'kind' | 'deviceId' | 'groupId' | 'label'>>,
  binding: { system_id_hash: string; label: string }
): Promise<Pick<MediaDeviceInfo, 'kind' | 'deviceId' | 'groupId' | 'label'> | null> {
  const audio = devices.filter((device) => device.kind === 'audioinput')
  const hashes = await Promise.all(audio.map((device) => mediaDeviceInfoHash(device)))
  const exactHash = hashes.indexOf(binding.system_id_hash)
  if (exactHash >= 0) return audio[exactHash]
  const label = normalizedDeviceName(binding.label)
  if (!label) return null
  const matches = audio.filter((device) => normalizedDeviceName(device.label) === label)
  return matches.length === 1 ? matches[0] : null
}

export type CompanionAudioDeviceMatch = {
  device: Pick<MediaDeviceInfo, 'kind' | 'deviceId' | 'groupId' | 'label'> | null
  method: 'persisted_microphone' | 'system_default'
}

/**
 * The companion is independent from cameras: Windows' current default input
 * is used unless a parent explicitly bound an available microphone.  A stale
 * binding falls back to the system default so wake listening survives USB
 * camera removal.  `device: null` deliberately means no exact deviceId
 * constraint should be sent to getUserMedia.
 */
export async function resolveCompanionAudioDevice(
  devices: Array<Pick<MediaDeviceInfo, 'kind' | 'deviceId' | 'groupId' | 'label'>>,
  microphoneBinding?: { system_id_hash: string; label: string } | null,
  _documentCameraBinding?: { system_id_hash: string; label: string } | null,
): Promise<CompanionAudioDeviceMatch | null> {
  if (microphoneBinding) {
    const device = await findBoundAudioDevice(devices, microphoneBinding)
    if (device) return { device, method: 'persisted_microphone' }
  }
  return { device: null, method: 'system_default' }
}

async function mediaDeviceIdentityHash(deviceId: string, _groupId: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(deviceId))
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

export function browserMediaErrorName(cause: unknown): string {
  if (cause instanceof DOMException || cause instanceof Error) return cause.name
  if (cause && typeof cause === 'object' && 'name' in cause && typeof cause.name === 'string') return cause.name
  return ''
}

export function browserMediaErrorMessage(cause: unknown): string {
  if (cause instanceof DOMException || cause instanceof Error) return cause.message
  if (cause && typeof cause === 'object' && 'message' in cause && typeof cause.message === 'string') return cause.message
  return ''
}

export function mediaDeviceFailure(cause: unknown, label: string): { code: string; message: string } {
  const name = browserMediaErrorName(cause)
  if (name === 'CameraStartTimeoutError') {
    return { code: 'DEVICE_START_TIMEOUT', message: `${label}启动超过20秒仍未响应，当前未开始读取画面；原请求结束后会自动重新检查。` }
  }
  if (name === 'CameraStartPendingError') {
    return { code: 'DEVICE_START_PENDING', message: `${label}上次启动仍未结束，当前未读取画面，正在等待设备响应。` }
  }
  if (name === 'NotAllowedError' || name === 'PermissionDeniedError') {
    return { code: 'MEDIA_PERMISSION_DENIED', message: `${label}权限被拒绝，请在Windows隐私设置中允许本应用使用该设备。` }
  }
  if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
    return { code: 'DEVICE_NOT_FOUND', message: `未找到${label}，请检查连接和系统设备列表。` }
  }
  if (name === 'NotReadableError' || name === 'TrackStartError') {
    return { code: 'DEVICE_BUSY_OR_UNAVAILABLE', message: `${label}可能正被其他程序占用，请关闭相关程序后重试。` }
  }
  if (name === 'OverconstrainedError') {
    return { code: 'UNSUPPORTED_DEVICE_MODE', message: `${label}不支持当前采集参数，应用需改用兼容模式。` }
  }
  return { code: 'DEVICE_OPEN_FAILED', message: `${label}未能打开，请检查权限、连接和其他程序占用情况。` }
}

export function isMediaDeviceError(cause: unknown): boolean {
  const name = browserMediaErrorName(cause)
  return ['NotAllowedError', 'PermissionDeniedError', 'NotFoundError', 'DevicesNotFoundError', 'NotReadableError', 'TrackStartError', 'OverconstrainedError', 'AbortError'].includes(name)
}

export type VideoHardwareMatch = {
  item: DesktopVideoHardwareItem
  method: 'exact_normalized_name' | 'unique_containment'
}

export function matchVideoHardwareInventory(label: string, items: DesktopVideoHardwareItem[]): VideoHardwareMatch | null {
  const normalizedLabel = normalizedDeviceName(label)
  if (!normalizedLabel) return null
  const exact = items.filter((item) => normalizedDeviceName(item.name) === normalizedLabel)
  if (exact.length === 1) return { item: exact[0], method: 'exact_normalized_name' }
  if (exact.length > 1) return null
  const contained = items.filter((item) => {
    const candidate = normalizedDeviceName(item.name)
    return candidate.length >= 5 && (normalizedLabel.includes(candidate) || candidate.includes(normalizedLabel))
  })
  return contained.length === 1 ? { item: contained[0], method: 'unique_containment' } : null
}

function numericRange(value: unknown): { min: number; max: number } | null {
  if (!value || typeof value !== 'object') return null
  const candidate = value as { min?: unknown; max?: unknown }
  return typeof candidate.min === 'number' && Number.isFinite(candidate.min)
    && typeof candidate.max === 'number' && Number.isFinite(candidate.max)
    ? { min: candidate.min, max: candidate.max }
    : null
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function normalizedDeviceName(value: string): string {
  return value.toLowerCase().replace(/\([^)]*\)/g, ' ').replace(/[^a-z0-9\u3400-\u9fff]+/g, ' ').trim().replace(/\s+/g, ' ')
}
