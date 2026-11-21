export type CameraDeviceOutput = {
  device_type: 'camera' | 'document_camera'
  device_status: 'active' | 'paused'
  frame_confirmed: boolean
  lease_confirmed: true
} & ({ observation_status: 'enabled' | 'paused' } | { persisted_status: 'active' | 'paused' })
export type DocumentInspectionOutput =
  | { artifact_id: string; revision_id: string; page_status: 'text_available' | 'empty' | 'blocked'; response_text: string }
  | { preview_id: string; page_status: 'partial_local_preview'; response_text: string }

export function partialDocumentInspectionReceipt(output: DocumentInspectionOutput) {
  if (output.page_status !== 'partial_local_preview') return null
  // The ordinary Runtime turn may be projected into later conversations.
  // Keep the actual OCR excerpt in the on-device reply and preview only.
  return {
    output: { preview_id: output.preview_id, page_status: output.page_status },
    recordedResponse: '这次已在本机试读，纸面文字仍需核对。',
  }
}

export type CameraDeviceCommand = {
  sessionId: string
  device: CameraDeviceOutput['device_type']
  command: 'pause' | 'resume'
  signal: AbortSignal
  resolve: (output: CameraDeviceOutput) => void
  reject: (error: Error) => void
}

export const CAMERA_DEVICE_COMMAND = 'hsc-camera-device-command'
export const CAMERA_DEVICE_STATUS = 'hsc-camera-device-status'
export type CameraDeviceStatus = {
  device_type: CameraDeviceOutput['device_type']
  track_live: boolean
  frame_confirmed: boolean
  last_frame_at: string | null
  last_error_code: string | null
  monitor_active: boolean
}
export type CameraStatusRequest = { sessionId: string; device: CameraDeviceOutput['device_type']; resolve: (status: CameraDeviceStatus) => void }

export class CameraFrameClock {
  private stream: MediaStream | null = null
  private progress: number | null = null
  private frameAt: number | null = null
  // UTC is a display timestamp. Frame health must survive system clock changes.
  private frameTick: number | null = null
  frameAgeMs(): number | null {
    return this.frameTick === null ? null : performance.now() - this.frameTick
  }
  /** Decoded frame identity for the current stream; null means it is unknown. */
  frameToken(): number | null {
    return this.progress
  }
  read(device: CameraDeviceOutput['device_type'], video: HTMLVideoElement | null, stream: MediaStream | null, active: boolean, error: string | null): CameraDeviceStatus {
    if (stream !== this.stream) { this.stream = stream; this.progress = null; this.frameAt = null; this.frameTick = stream ? performance.now() : null }
    const tracks = stream?.getVideoTracks() ?? []
    const live = tracks.some((track) => track.readyState === 'live')
    const readable = tracks.some((track) => track.readyState === 'live' && track.enabled && !track.muted)
    const usable = readable && video?.srcObject === stream && !video.paused && !video.ended
      && video.readyState >= 2 && video.videoWidth > 0 && video.videoHeight > 0
    if (usable) {
      // Chromium's media clock can advance without new source frames. Count
      // received frames, including those not displayed when the preview is
      // offscreen; subtracting dropped frames falsely retires a live camera.
      // A media clock alone is not evidence that the camera delivered a new
      // frame. If the frame counter is unavailable, leave freshness unknown
      // and let the existing watchdog recover rather than accept stale video.
      let progress: number | null = null
      try {
        const quality = video.getVideoPlaybackQuality?.()
        const frames = quality?.totalVideoFrames
        if (typeof frames === 'number' && Number.isFinite(frames)) {
          progress = frames
        }
      } catch { /* Older surfaces may expose no usable playback counter. */ }
      if (progress !== null && progress !== this.progress) { this.progress = progress; this.frameAt = Date.now(); this.frameTick = performance.now() }
    }
    const frameAge = this.frameAgeMs()
    const fresh = usable && this.frameAt !== null && frameAge !== null && frameAge < 5_000
    return { device_type: device, track_live: live, frame_confirmed: Boolean(fresh), last_frame_at: this.frameAt === null ? null : new Date(this.frameAt).toISOString(),
      last_error_code: error, monitor_active: Boolean(active && fresh) }
  }
}

export function queryCameraDevices(sessionId: string, device: 'camera' | 'document_camera' | 'all'): { devices: CameraDeviceStatus[]; checked_at: string } {
  const devices = (device === 'all' ? ['camera', 'document_camera'] as const : [device]).map((type) => {
    let result: CameraDeviceStatus = { device_type: type, track_live: false, frame_confirmed: false, last_frame_at: null, last_error_code: 'DEVICE_NOT_ACTIVE', monitor_active: false }
    window.dispatchEvent(new CustomEvent(CAMERA_DEVICE_STATUS, { detail: { sessionId, device: type, resolve: (status: CameraDeviceStatus) => {
      // The continuous monitor may be mounted but yielded to a single capture.
      if (status.track_live || !result.track_live) result = status
    } } satisfies CameraStatusRequest }))
    return result
  })
  return { devices, checked_at: new Date().toISOString() }
}

export function requestCameraDevice(sessionId: string, device: CameraDeviceOutput['device_type'], command: 'pause' | 'resume', signal?: AbortSignal): Promise<CameraDeviceOutput> {
  return new Promise((resolve, reject) => {
    const controller = new AbortController()
    let settled = false
    const finish = (output?: CameraDeviceOutput, error?: Error): void => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      signal?.removeEventListener('abort', abort)
      if (error) { controller.abort(); reject(error) } else resolve(output!)
    }
    const abort = (): void => finish(undefined, new DOMException('Device command cancelled', 'AbortError'))
    const timer = setTimeout(() => finish(undefined, new Error('DEVICE_RESULT_UNCONFIRMED')), 15_000)
    if (signal?.aborted) { abort(); return }
    signal?.addEventListener('abort', abort, { once: true })
    const detail: CameraDeviceCommand = { sessionId, device, command, signal: controller.signal,
      resolve: (output) => finish(output), reject: (error) => finish(undefined, error) }
    const event = new CustomEvent(CAMERA_DEVICE_COMMAND, { cancelable: true, detail })
    window.dispatchEvent(event)
    if (!event.defaultPrevented) finish(undefined, new Error('DEVICE_RECEIVER_UNAVAILABLE'))
  })
}

type CameraFrameSource = { video: HTMLVideoElement | null; stream: MediaStream | null; leased: boolean; generation: number }

export function waitForCameraFrame(read: () => CameraFrameSource, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const started = performance.now()
    const generation = read().generation
    let video: HTMLVideoElement | null = null
    let stream: MediaStream | null = null
    let track: MediaStreamTrack | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    let callback: number | null = null
    let callbackEpoch = 0
    let settled = false
    let mode: 'quality' | 'callback' | null = null
    let baseline: number | null = null
    let presented: { frames: number; time: number } | null = null
    const cancelCallback = (): void => {
      callbackEpoch += 1
      if (callback !== null) {
        try { video?.cancelVideoFrameCallback?.(callback) } catch { /* Best effort on the original video only. */ }
        callback = null
      }
    }
    const finish = (error?: Error): void => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      cancelCallback()
      signal.removeEventListener('abort', abort)
      if (error) reject(error); else resolve()
    }
    const abort = (): void => finish(new DOMException('Device command cancelled', 'AbortError'))
    const current = (): CameraFrameSource | null => {
      const state = read()
      const nextTrack = state.stream?.getVideoTracks()[0] ?? null
      if (state.generation !== generation || (video && state.video !== video) || (stream && state.stream !== stream) || (track && nextTrack !== track)) {
        abort(); return null
      }
      video ??= state.video
      stream ??= state.stream
      track ??= nextTrack
      return state
    }
    const usable = (state: CameraFrameSource): boolean => Boolean(state.leased && track?.readyState === 'live' && track.enabled && !track.muted
      && video?.srcObject === stream && !video.paused && !video.ended
      && video.readyState >= 2 && video.videoWidth > 0 && video.videoHeight > 0)
    const frameCount = (): number | null => {
      try {
        const frames = video?.getVideoPlaybackQuality?.().totalVideoFrames
        return typeof frames === 'number' && Number.isFinite(frames) && frames >= 0 ? frames : null
      } catch { return null }
    }
    const expired = (): boolean => performance.now() - started >= 5_000
    const armCallback = (): void => {
      if (settled || callback !== null || !video?.requestVideoFrameCallback) return
      const epoch = ++callbackEpoch
      callback = video.requestVideoFrameCallback((_now, metadata) => {
        if (settled || epoch !== callbackEpoch) return
        callbackEpoch += 1
        callback = null
        if (signal.aborted) { abort(); return }
        const state = current()
        if (!state) return
        if (expired()) { finish(new Error('DEVICE_NO_CONFIRMED_FRAME')); return }
        if (!usable(state)) { presented = null; return }
        const frames = metadata.presentedFrames, time = metadata.mediaTime
        if (Number.isFinite(frames) && frames >= 0 && Number.isFinite(time) && time >= 0) {
          // The first callback may describe a frame cached before the command.
          // Only a subsequent, strictly newer frame on this source can confirm.
          if (presented && frames > presented.frames && time > presented.time) { finish(); return }
          presented ??= { frames, time }
        }
        armCallback()
      })
    }
    const check = (): void => {
      if (settled) return
      if (signal.aborted) { abort(); return }
      const state = current()
      if (!state) return
      if (expired()) { finish(new Error('DEVICE_NO_CONFIRMED_FRAME')); return }
      if (!usable(state)) {
        baseline = null; presented = null; cancelCallback()
      } else {
        const frames = frameCount()
        mode ??= frames === null ? 'callback' : 'quality'
        if (mode === 'quality') {
          if (frames !== null && baseline !== null && frames > baseline) { finish(); return }
          baseline ??= frames
        } else armCallback()
      }
      timer = setTimeout(check, 50)
    }
    signal.addEventListener('abort', abort, { once: true })
    check()
  })
}
