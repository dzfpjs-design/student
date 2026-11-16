import { afterEach, describe, expect, it, vi } from 'vitest'
import { qualificationFailure, qualificationRoles, runVideoQualification, type QualificationMetrics } from './deviceQualification'
import type { DeviceBinding } from './types'
import { observationCameraVideoConstraints, documentCameraVideoConstraints } from './mediaDevices'

vi.mock('./mediaDevices', async (importOriginal) => ({
  ...await importOriginal<typeof import('./mediaDevices')>(),
  resolveBoundVideoDevice: vi.fn(async (devices: MediaDeviceInfo[], _hash: string, label: string) => ({ method: 'system_id_hash', device: devices.find((device) => device.label === label)! })),
  mediaDeviceRegistration: vi.fn(async (track: MediaStreamTrack) => ({ systemIdHash: `${track.label}-hash`, capabilities: {} })),
  mediaDeviceFailure: vi.fn(() => ({ code: 'MEDIA_STARTUP_FAILED' })),
}))

const healthy = (overrides: Partial<QualificationMetrics> = {}): QualificationMetrics => ({
  elapsed_seconds: 60,
  expected_seconds: 60,
  sample_interval_ms: 1000,
  devices: {
    camera: { samples: 60, advancing_samples: 59, frozen_samples: 1, max_consecutive_frozen: 1, frame_count: 1500, frame_callback_supported: true, content_change_samples: 30, low_variation_samples: 29, ended: false, width: 1920, height: 1080, frame_rate: 30 },
    document_camera: { samples: 60, advancing_samples: 60, frozen_samples: 0, max_consecutive_frozen: 0, frame_count: 1500, frame_callback_supported: true, content_change_samples: 2, low_variation_samples: 57, ended: false, width: 2560, height: 1440, frame_rate: 25 },
  },
  ...overrides,
})

describe('actual qualification capture window', () => {
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers() })

  const setup = (failSecond = false) => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] })
    const stops = [vi.fn(), vi.fn()]
    const roles = ['camera', 'document_camera'] as const
    const tracks = roles.map((role, index) => ({ label: role, readyState: 'live', stop: stops[index], addEventListener: vi.fn(), getSettings: () => ({ width: 1920, height: 1080, frameRate: 30 }) }))
    const streams = tracks.map((track) => ({ getVideoTracks: () => [track], getTracks: () => [track] }))
    let request = 0
    const getUserMedia = vi.fn(async () => {
      const index = request++
      await new Promise<void>((resolve) => window.setTimeout(resolve, 1500))
      if (failSecond && index === 1) throw new Error('synthetic second camera startup failure')
      return streams[index] as unknown as MediaStream
    })
    vi.stubGlobal('navigator', { mediaDevices: { getUserMedia, enumerateDevices: vi.fn(async () => roles.map((role) => ({ label: role, deviceId: role, kind: 'videoinput' }))) } })
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    vi.spyOn(HTMLMediaElement.prototype, 'currentTime', 'get').mockImplementation(() => performance.now() / 1000)
    const bindings = roles.map((role) => ({ device_type: role, label: role, system_id_hash: `${role}-hash` })) as unknown as DeviceBinding[]
    const controller = new AbortController(), onProgress = vi.fn()
    const run = () => runVideoQualification({ mode: 'quick_dual', bindings, expectedSeconds: 60, signal: controller.signal, onProgress })
    return { run, stops, controller, onProgress, getUserMedia, streams, bindings }
  }

  it('captures the full 60-second dual sample window after three seconds of actual sequential startup', async () => {
    const fixture = setup(), task = fixture.run()
    await vi.advanceTimersByTimeAsync(63_000)
    const result = await task
    expect(result).toMatchObject({ outcome: 'passed', metrics: { elapsed_seconds: 60, expected_seconds: 60, devices: {
      camera: { samples: 60, advancing_samples: 60, frozen_samples: 0 },
      document_camera: { samples: 60, advancing_samples: 60, frozen_samples: 0 },
    } } })
    expect(fixture.onProgress).toHaveBeenCalledTimes(60)
    expect(fixture.getUserMedia).toHaveBeenCalledTimes(2)
    fixture.stops.forEach((stop) => expect(stop).toHaveBeenCalledTimes(1))
    expect(document.querySelectorAll('video')).toHaveLength(0)
  })

  it('qualifies the same bound capture profiles used for observation and paper capture', async () => {
    const fixture = setup(), task = fixture.run()
    await vi.advanceTimersByTimeAsync(63_000)
    expect((await task).outcome).toBe('passed')
    expect(fixture.getUserMedia).toHaveBeenNthCalledWith(1, {
      audio: false, video: observationCameraVideoConstraints('camera', 'camera'),
    })
    expect(fixture.getUserMedia).toHaveBeenNthCalledWith(2, {
      audio: false, video: documentCameraVideoConstraints('document_camera'),
    })
  })

  it('uses the scanner resolution ladder when the highest production mode is unsupported', async () => {
    const fixture = setup()
    fixture.getUserMedia.mockResolvedValueOnce(fixture.streams[0] as unknown as MediaStream)
      .mockRejectedValueOnce(new DOMException('unsupported mode', 'OverconstrainedError'))
      .mockResolvedValueOnce(fixture.streams[1] as unknown as MediaStream)
    const task = fixture.run()
    await vi.advanceTimersByTimeAsync(60_000)
    expect((await task).outcome).toBe('passed')
    expect(fixture.getUserMedia).toHaveBeenCalledTimes(3)
    expect(fixture.getUserMedia).toHaveBeenNthCalledWith(3, {
      audio: false, video: documentCameraVideoConstraints('document_camera', 'capture', { width: 3840, height: 2160 }),
    })
    fixture.stops.forEach(stop => expect(stop).toHaveBeenCalledTimes(1))
  })

  it('retains a native acquisition after the shared opener deadline and stops its late stream before settling', async () => {
    const fixture = setup()
    let resolveLate!: (stream: MediaStream) => void
    const late = new Promise<MediaStream>(resolve => { resolveLate = resolve })
    fixture.getUserMedia.mockReturnValueOnce(late)
    let settled = false
    const task = fixture.run().then(result => { settled = true; return result })
    try {
      await vi.advanceTimersByTimeAsync(21_000)
      expect(settled).toBe(false)
      const replacement = await runVideoQualification({ mode: 'quick_dual', bindings: fixture.bindings,
        expectedSeconds: 60, signal: new AbortController().signal, onProgress: vi.fn() })
      expect(replacement.failureCode).toBe('DEVICE_ACQUISITION_PENDING')
      expect(fixture.getUserMedia).toHaveBeenCalledTimes(1)
      resolveLate(fixture.streams[0] as unknown as MediaStream)
      await vi.advanceTimersByTimeAsync(0)
      expect((await task).outcome).toBe('failed')
      expect(fixture.stops[0]).toHaveBeenCalledTimes(1)
      expect(fixture.stops[1]).not.toHaveBeenCalled()
      expect(document.querySelectorAll('video')).toHaveLength(0)
    } finally {
      resolveLate(fixture.streams[0] as unknown as MediaStream)
      await task
    }
  })

  it('continues sampling until the full window even when the minimum sample threshold is already met', async () => {
    const fixture = setup()
    let settled = false
    const task = fixture.run().then((value) => { settled = true; return value })
    await vi.advanceTimersByTimeAsync(62_000)
    expect(fixture.onProgress).toHaveBeenCalledTimes(59)
    expect(settled).toBe(false)
    await vi.advanceTimersByTimeAsync(1_000)
    expect((await task).outcome).toBe('passed')
  })

  it('does not shorten the sample count when per-sample processing consumes 100ms', async () => {
    const fixture = setup()
    fixture.onProgress.mockImplementation(() => { vi.advanceTimersByTime(100) })
    const task = fixture.run()
    await vi.advanceTimersByTimeAsync(70_000)
    const result = await task
    expect(result).toMatchObject({ outcome: 'passed', metrics: { devices: { camera: { samples: 60 }, document_camera: { samples: 60 } } } })
    expect(result.metrics.elapsed_seconds).toBe(60)
    fixture.stops.forEach((stop) => expect(stop).toHaveBeenCalledTimes(1))
  })

  it('keeps genuinely missed periods missing and rejects sparse evidence without reporting a frame stall', async () => {
    const fixture = setup()
    fixture.onProgress.mockImplementation(() => { vi.advanceTimersByTime(1500) })
    const task = fixture.run()
    await vi.advanceTimersByTimeAsync(80_000)
    const result = await task
    expect(result.outcome).toBe('failed')
    expect(result.failureCode).toBe('INSUFFICIENT_FRAME_SAMPLES')
    for (const metrics of Object.values(result.metrics.devices)) {
      expect(metrics!.samples).toBe(30)
      expect(metrics!.advancing_samples).toBe(30)
      expect(metrics!.frozen_samples).toBe(0)
    }
    fixture.stops.forEach((stop) => expect(stop).toHaveBeenCalledTimes(1))
  })

  it('reports only the measured capture window on cancellation and stops both original streams', async () => {
    const fixture = setup(), task = fixture.run()
    await vi.advanceTimersByTimeAsync(5_000)
    fixture.controller.abort()
    const result = await task
    expect(result).toMatchObject({ outcome: 'cancelled', failureCode: 'USER_CANCELLED', metrics: { elapsed_seconds: 2, devices: { camera: { samples: 2 }, document_camera: { samples: 2 } } } })
    fixture.stops.forEach((stop) => expect(stop).toHaveBeenCalledTimes(1))
    expect(document.querySelectorAll('video')).toHaveLength(0)
  })

  it('does not count a failed device startup as captured time and releases the already-open first device', async () => {
    const fixture = setup(true), task = fixture.run()
    await vi.advanceTimersByTimeAsync(3_000)
    const result = await task
    expect(result).toMatchObject({ outcome: 'failed', failureCode: 'MEDIA_STARTUP_FAILED', metrics: { elapsed_seconds: 0, devices: { camera: { samples: 0 } } } })
    expect(fixture.stops[0]).toHaveBeenCalledTimes(1)
    expect(fixture.stops[1]).not.toHaveBeenCalled()
    expect(fixture.onProgress).not.toHaveBeenCalled()
    expect(document.querySelectorAll('video')).toHaveLength(0)
  })

  it('cancels a pending play immediately, releases its owned stream and never opens the next device', async () => {
    const fixture = setup()
    let resolvePlay!: () => void
    const play = new Promise<void>((resolve) => { resolvePlay = resolve })
    vi.mocked(HTMLMediaElement.prototype.play).mockReturnValueOnce(play)
    const task = fixture.run()
    try {
      await vi.advanceTimersByTimeAsync(1500)
      fixture.controller.abort()
      expect(fixture.stops[0]).toHaveBeenCalledTimes(1)
      await vi.advanceTimersByTimeAsync(0)
      expect(await task).toMatchObject({ outcome: 'cancelled', failureCode: 'USER_CANCELLED', metrics: { elapsed_seconds: 0 } })
      expect(fixture.getUserMedia).toHaveBeenCalledTimes(1)
      expect(document.querySelectorAll('video')).toHaveLength(0)
    } finally {
      resolvePlay()
      await vi.advanceTimersByTimeAsync(3000)
      await task
    }
    expect(fixture.getUserMedia).toHaveBeenCalledTimes(1)
    fixture.stops.forEach((stop, index) => expect(stop).toHaveBeenCalledTimes(index === 0 ? 1 : 0))
  })

  it('stops known streams on cancel but keeps an unresolved native acquisition pending until its owned late stream is closed', async () => {
    const fixture = setup()
    let resolveLate!: (stream: MediaStream) => void
    const late = new Promise<MediaStream>((resolve) => { resolveLate = resolve })
    fixture.getUserMedia.mockResolvedValueOnce(fixture.streams[0] as unknown as MediaStream).mockReturnValueOnce(late)
    let settled = false
    const task = fixture.run().then((value) => { settled = true; return value })
    try {
      await vi.advanceTimersByTimeAsync(0)
      expect(fixture.getUserMedia).toHaveBeenCalledTimes(2)
      fixture.controller.abort()
      expect(fixture.stops[0]).toHaveBeenCalledTimes(1)
      await vi.advanceTimersByTimeAsync(30_000)
      expect(settled).toBe(false)
      expect(fixture.stops[1]).not.toHaveBeenCalled()
      expect(document.querySelector('video')?.srcObject ?? null).toBeNull()
      const replacement = await runVideoQualification({ mode: 'quick_dual', bindings: fixture.bindings, expectedSeconds: 60, signal: new AbortController().signal, onProgress: vi.fn() })
      expect(replacement).toMatchObject({ outcome: 'failed', failureCode: 'DEVICE_ACQUISITION_PENDING' })
      expect(fixture.getUserMedia).toHaveBeenCalledTimes(2)
      resolveLate(fixture.streams[1] as unknown as MediaStream)
      await vi.advanceTimersByTimeAsync(0)
      expect(await task).toMatchObject({ outcome: 'cancelled', failureCode: 'USER_CANCELLED', metrics: { elapsed_seconds: 0 } })
      expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1)
      expect(fixture.getUserMedia).toHaveBeenCalledTimes(2)
      fixture.stops.forEach((stop) => expect(stop).toHaveBeenCalledTimes(1))
      expect(document.querySelectorAll('video')).toHaveLength(0)
    } finally {
      resolveLate(fixture.streams[1] as unknown as MediaStream)
      await vi.advanceTimersByTimeAsync(3000)
      await task
    }
  })

  it('cancels hung registration after acquisition and releases the stream before metadata can settle', async () => {
    const fixture = setup(), media = await import('./mediaDevices')
    let resolveRegistration!: (value: Awaited<ReturnType<typeof media.mediaDeviceRegistration>>) => void
    const registration = new Promise<Awaited<ReturnType<typeof media.mediaDeviceRegistration>>>((resolve) => { resolveRegistration = resolve })
    vi.mocked(media.mediaDeviceRegistration).mockReturnValueOnce(registration)
    const task = fixture.run()
    try {
      await vi.advanceTimersByTimeAsync(1500)
      fixture.controller.abort()
      expect(fixture.stops[0]).toHaveBeenCalledTimes(1)
      await vi.advanceTimersByTimeAsync(0)
      expect(await task).toMatchObject({ outcome: 'cancelled', failureCode: 'USER_CANCELLED', metrics: { elapsed_seconds: 0 } })
      expect(fixture.getUserMedia).toHaveBeenCalledTimes(1)
      expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled()
    } finally {
      resolveRegistration({ systemIdHash: 'camera-hash', capabilities: {} })
      await vi.advanceTimersByTimeAsync(3000)
      await task
    }
    expect(fixture.getUserMedia).toHaveBeenCalledTimes(1)
    fixture.stops.forEach((stop, index) => expect(stop).toHaveBeenCalledTimes(index === 0 ? 1 : 0))
  })

  it('preserves a play rejection even when its reason is undefined and releases acquired media', async () => {
    const fixture = setup()
    vi.mocked(HTMLMediaElement.prototype.play).mockRejectedValueOnce(undefined)
    const task = fixture.run()
    await vi.advanceTimersByTimeAsync(1500)
    expect(await task).toMatchObject({ outcome: 'failed', failureCode: 'MEDIA_STARTUP_FAILED', metrics: { elapsed_seconds: 0 } })
    expect(fixture.stops[0]).toHaveBeenCalledTimes(1)
    expect(fixture.getUserMedia).toHaveBeenCalledTimes(1)
    expect(document.querySelectorAll('video')).toHaveLength(0)
  })
})

describe('device qualification metrics', () => {
  it('passes bounded startup freezes and maps modes to exact roles', () => {
    expect(qualificationFailure(healthy())).toBeNull()
    expect(qualificationRoles('dual_concurrency')).toEqual(['camera', 'document_camera'])
    expect(qualificationRoles('camera_stability')).toEqual(['camera'])
    expect(qualificationRoles('document_stability')).toEqual(['document_camera'])
  })

  it('fails incomplete, disconnected and repeatedly stalled streams', () => {
    expect(qualificationFailure(healthy({ elapsed_seconds: 20 }))).toBe('TEST_INCOMPLETE')
    expect(qualificationFailure(healthy({ devices: { camera: { ...healthy().devices.camera!, ended: true } } }))).toBe('DEVICE_DISCONNECTED')
    expect(qualificationFailure(healthy({ devices: { camera: { ...healthy().devices.camera!, frozen_samples: 10, advancing_samples: 50, max_consecutive_frozen: 5 } } }))).toBe('FRAME_STALLED')
  })

  it('reports a disconnected camera when the measured window ends early', () => {
    expect(qualificationFailure(healthy({ elapsed_seconds: 55, devices: {
      camera: { ...healthy().devices.camera!, samples: 55, advancing_samples: 46,
        frozen_samples: 9, max_consecutive_frozen: 9, ended: true },
      document_camera: { ...healthy().devices.document_camera!, samples: 55, advancing_samples: 55 },
    } }))).toBe('DEVICE_DISCONNECTED')
  })

  it('rejects sparse, contradictory and capability-free evidence', () => {
    expect(qualificationFailure(healthy({ devices: { camera: { ...healthy().devices.camera!, samples: 10, advancing_samples: 10, frozen_samples: 0 } } }))).toBe('INSUFFICIENT_FRAME_SAMPLES')
    expect(qualificationFailure(healthy({ devices: { camera: { ...healthy().devices.camera!, advancing_samples: 58, frozen_samples: 1 } } }))).toBe('INVALID_FRAME_METRICS')
    expect(qualificationFailure(healthy({ devices: { camera: { ...healthy().devices.camera!, width: null } } }))).toBe('DEVICE_CAPABILITIES_INCOMPLETE')
  })

  it('records a static document scene without treating valid new frames as a stalled stream', () => {
    expect(qualificationFailure(healthy({ devices: {
      document_camera: { ...healthy().devices.document_camera!, content_change_samples: 0, low_variation_samples: 59 }
    } }))).toBeNull()
  })
})
