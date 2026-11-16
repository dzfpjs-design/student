import { act, cleanup, render } from '@testing-library/react'
import { createHash } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { LocalObservation } from './SessionWorkspace'
import { CameraFrameClock, queryCameraDevices, requestCameraDevice, waitForCameraFrame } from './cameraDeviceRuntime'
import * as media from './mediaDevices'
import { reconcileObservationRegistration } from './observationCameraStartup'
import { reconcileObservationCoverage } from './observationCoverageStartup'

const coverageReceipt = (epoch: string, boundary: 'start' | 'stop') => ({ status: 'recorded', event_id: `coverage-action:${createHash('sha256').update(`coverage-${boundary}:${epoch}`).digest('hex')}` })

describe('observational camera physical execution receipts', () => {
  let frameReady: boolean
  let mediaTime: number
  let stops: Array<ReturnType<typeof vi.fn>>
  let recoverOriginalCoverage: (() => void) | null
  const originalMedia = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
  const originalCallback = Object.getOwnPropertyDescriptor(HTMLVideoElement.prototype, 'requestVideoFrameCallback')
  const originalCancel = Object.getOwnPropertyDescriptor(HTMLVideoElement.prototype, 'cancelVideoFrameCallback')
  beforeEach(() => {
    vi.useFakeTimers(); frameReady = false; mediaTime = 0; stops = []; recoverOriginalCoverage = null
    vi.spyOn(crypto.subtle, 'digest').mockImplementation(async (_algorithm, data) => Uint8Array.from(createHash('sha256').update(Buffer.from(data as ArrayBuffer)).digest()).buffer)
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue()
    // A fulfilled mocked play() must also model the real playing DOM state.
    vi.spyOn(HTMLMediaElement.prototype, 'paused', 'get').mockReturnValue(false)
    vi.spyOn(HTMLMediaElement.prototype, 'ended', 'get').mockReturnValue(false)
    vi.spyOn(HTMLMediaElement.prototype, 'readyState', 'get').mockImplementation(() => frameReady ? 2 : 0)
    vi.spyOn(HTMLMediaElement.prototype, 'currentTime', 'get').mockImplementation(() => mediaTime)
    // This legacy surface has no playback-quality counter; model real new-frame callbacks.
    Object.defineProperty(HTMLVideoElement.prototype, 'requestVideoFrameCallback', { configurable: true, value: (fn: VideoFrameRequestCallback) => window.setTimeout(() => { if (frameReady) fn(performance.now(), { presentedFrames: performance.now(), mediaTime: performance.now() / 1000 } as VideoFrameCallbackMetadata) }, 25) })
    Object.defineProperty(HTMLVideoElement.prototype, 'cancelVideoFrameCallback', { configurable: true, value: (id: number) => window.clearTimeout(id) })
    vi.spyOn(HTMLVideoElement.prototype, 'videoWidth', 'get').mockImplementation(() => frameReady ? 640 : 0)
    vi.spyOn(HTMLVideoElement.prototype, 'videoHeight', 'get').mockImplementation(() => frameReady ? 360 : 0)
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      enumerateDevices: vi.fn().mockResolvedValue([]),
      getUserMedia: vi.fn(async () => {
        const track = { readyState: 'live', enabled: true, muted: false, stop: vi.fn(() => { track.readyState = 'ended' }), addEventListener: vi.fn() }
        stops.push(track.stop)
        return { getTracks: () => [track], getVideoTracks: () => [track] }
      }),
    } })
    vi.spyOn(media, 'resolveBoundVideoDevice').mockResolvedValue({ device: { deviceId: 'bound-camera' } as MediaDeviceInfo, method: 'system_id_hash' })
    vi.spyOn(media, 'mediaDeviceRegistration').mockResolvedValue({ systemIdHash: 'camera-hash', capabilities: {} })
    vi.spyOn(localApi, 'deviceBindings').mockResolvedValue([{ device_type: 'camera', system_id_hash: 'camera-hash', label: 'camera' }] as never)
    vi.spyOn(localApi, 'resumeObservation').mockResolvedValue({ observation_status: 'enabled' } as never)
    vi.spyOn(localApi, 'pauseObservation').mockResolvedValue({ observation_status: 'paused' } as never)
    vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'camera-lease', status: 'active', expires_at: 'later' })
    vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'camera-lease', status: 'released' })
    vi.spyOn(localApi, 'startObservationQualification').mockResolvedValue({} as never)
    vi.spyOn(localApi, 'beginObservationStream').mockImplementation(async (_session, epoch) => coverageReceipt(epoch, 'start'))
    vi.spyOn(localApi, 'endObservationStream').mockImplementation(async (_session, epoch) => coverageReceipt(epoch, 'stop'))
    vi.spyOn(localApi, 'recordDeviceIncident').mockResolvedValue({} as never)
    vi.spyOn(localApi, 'pendingSessionBehaviorIntervention').mockResolvedValue({ status: 'none' } as never)
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('UNMOCKED_NETWORK')))
  })
  afterEach(async () => {
    // Retained owners survive unmount. Recover real fixture receipts before
    // restoring mocks, rather than clearing production quarantine maps.
    vi.mocked(localApi.releaseDeviceLease).mockImplementation(async leaseId => ({ lease_id: leaseId, status: 'released' }))
    vi.mocked(localApi.endObservationStream).mockImplementation(async (_session, epoch) => coverageReceipt(epoch, 'stop'))
    if (recoverOriginalCoverage) await act(async () => { recoverOriginalCoverage?.() })
    cleanup()
    await act(async () => { await vi.advanceTimersByTimeAsync(5_100) })
    await act(async () => { await reconcileObservationRegistration('camera-hash') })
    for (const session of ['synthetic-session', 'old-synthetic-session', 'new-synthetic-session']) await act(async () => { await reconcileObservationCoverage(session) })
    vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers()
    if (originalMedia) Object.defineProperty(navigator, 'mediaDevices', originalMedia)
    for (const [key, value] of [['requestVideoFrameCallback', originalCallback], ['cancelVideoFrameCallback', originalCancel]] as const) {
      if (value) Object.defineProperty(HTMLVideoElement.prototype, key, value); else Reflect.deleteProperty(HTMLVideoElement.prototype, key)
    }
  })
  const mount = () => render(<LocalObservation sessionId="synthetic-session" active enabled={false} snoozeUntil={null} />)

  const automatic = (sessionId = 'synthetic-session') => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
    return render(<LocalObservation sessionId={sessionId} active enabled snoozeUntil={null} />)
  }

  it('keeps an immediately available, advancing camera alive beyond the first-frame deadline', async () => {
    frameReady = true
    automatic()
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    for (let second = 1; second <= 18; second++) {
      mediaTime = second
      await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    }
    expect(stops[0]).not.toHaveBeenCalled()
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    expect(queryCameraDevices('synthetic-session', 'camera').devices[0]).toMatchObject({ track_live: true, frame_confirmed: true })
    expect(localApi.recordDeviceIncident).not.toHaveBeenCalledWith('synthetic-session', 'camera', 'NO_USABLE_VIDEO_FRAME', expect.any(String))
  })

  it('retires and reconnects a silent stalled camera without an ended event or child action', async () => {
    frameReady = true
    automatic()
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    await act(async () => { await vi.advanceTimersByTimeAsync(6_100) })
    expect(stops[0]).toHaveBeenCalledTimes(1)
    expect(localApi.recordDeviceIncident).toHaveBeenCalledWith('synthetic-session', 'camera', 'CAMERA_FRAME_STALLED', expect.any(String))
    mediaTime = 10
    await act(async () => { await vi.advanceTimersByTimeAsync(2_500) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
    expect(queryCameraDevices('synthetic-session', 'camera').devices[0]).toMatchObject({ track_live: true, frame_confirmed: true })
  })

  it('retires a paused preview even while decoded frames advance', async () => {
    frameReady = true
    let paused = false
    let frames = 12
    vi.spyOn(HTMLMediaElement.prototype, 'paused', 'get').mockImplementation(() => paused)
    vi.mocked(HTMLMediaElement.prototype.play).mockImplementation(async () => { paused = false })
    const view = automatic()
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    const video = view.getByLabelText('观察摄像头实时画面')
    Object.defineProperty(video, 'getVideoPlaybackQuality', { configurable: true,
      value: () => ({ totalVideoFrames: frames, droppedVideoFrames: frames - 12 }) })
    expect(queryCameraDevices('synthetic-session', 'camera').devices[0].frame_confirmed).toBe(true)
    paused = true
    for (let second = 1; second <= 6; second++) {
      frames += 8; mediaTime = second
      await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    }
    expect(stops[0]).toHaveBeenCalledTimes(1)
    expect(localApi.recordDeviceIncident).toHaveBeenCalledWith('synthetic-session', 'camera', 'CAMERA_FRAME_STALLED', expect.any(String))
    await act(async () => { await vi.advanceTimersByTimeAsync(2_500) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
    expect(queryCameraDevices('synthetic-session', 'camera').devices[0]).toMatchObject({ track_live: true, frame_confirmed: true })
    expect(stops[0]).toHaveBeenCalledTimes(1)
  })

  it.each(['paused', 'ended'] as const)('retains the automatic twelve-second first-frame deadline while initially %s', async (state) => {
    frameReady = true
    vi.spyOn(HTMLMediaElement.prototype, state, 'get').mockReturnValue(true)
    const confirm = vi.spyOn(localApi, 'confirmDeviceFrame').mockResolvedValue({} as never)
    automatic()
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    expect(confirm).not.toHaveBeenCalled()
    expect(queryCameraDevices('synthetic-session', 'camera').devices[0]).toMatchObject({
      track_live: true, frame_confirmed: false, last_frame_at: null })
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(stops[0]).not.toHaveBeenCalled()
    await act(async () => { await vi.advanceTimersByTimeAsync(2_100) })
    expect(confirm).not.toHaveBeenCalled()
    expect(stops[0]).toHaveBeenCalledTimes(1)
    expect(localApi.recordDeviceIncident).toHaveBeenCalledWith('synthetic-session', 'camera', 'NO_USABLE_VIDEO_FRAME', expect.any(String))
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
  })

  it.each(['paused', 'ended'] as const)('confirms the automatic first frame only after the initial %s state clears', async (state) => {
    frameReady = true
    let inactive = true
    vi.spyOn(HTMLMediaElement.prototype, state, 'get').mockImplementation(() => inactive)
    const confirm = vi.spyOn(localApi, 'confirmDeviceFrame').mockResolvedValue({} as never)
    automatic()
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    expect(confirm).not.toHaveBeenCalled()
    inactive = false
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    expect(confirm).toHaveBeenCalledExactlyOnceWith('camera-lease', 640, 360)
    expect(queryCameraDevices('synthetic-session', 'camera').devices[0]).toMatchObject({ track_live: true, frame_confirmed: true })
    for (let second = 1; second <= 13; second++) {
      mediaTime++
      await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    }
    expect(stops[0]).not.toHaveBeenCalled()
    expect(localApi.recordDeviceIncident).not.toHaveBeenCalledWith('synthetic-session', 'camera', 'NO_USABLE_VIDEO_FRAME', expect.any(String))
  })

  it('bounds the first-frame wait even when video.play never settles', async () => {
    vi.mocked(HTMLMediaElement.prototype.play).mockImplementation(() => new Promise<void>(() => {}))
    automatic()
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    await act(async () => { await vi.advanceTimersByTimeAsync(12_100) })
    expect(stops[0]).toHaveBeenCalledTimes(1)
    expect(localApi.recordDeviceIncident).toHaveBeenCalledWith('synthetic-session', 'camera', 'NO_USABLE_VIDEO_FRAME', expect.any(String))
  })

  it('retires a hung registration, retains its late lease, and waits for real release before reopening', async () => {
    let registered!: (lease: { lease_id: string; status: string; expires_at: string }) => void
    let released!: (lease: { lease_id: string; status: string }) => void
    vi.mocked(localApi.registerDevice).mockReturnValueOnce(new Promise(resolve => { registered = resolve }))
    vi.mocked(localApi.releaseDeviceLease).mockReturnValueOnce(new Promise(resolve => { released = resolve }))
    automatic()
    await act(async () => { await vi.advanceTimersByTimeAsync(12_100) })
    expect(stops[0]).toHaveBeenCalledTimes(1)
    expect(localApi.recordDeviceIncident).toHaveBeenCalledWith('synthetic-session', 'camera', 'CAMERA_STARTUP_TIMEOUT', expect.any(String))
    await act(async () => { await vi.advanceTimersByTimeAsync(40_000) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    expect(localApi.startObservationQualification).not.toHaveBeenCalled()
    await act(async () => { registered({ lease_id: 'late-original-lease', status: 'active', expires_at: 'later' }) })
    expect(localApi.releaseDeviceLease).toHaveBeenCalledWith('late-original-lease', 'observation_registration_retired', expect.any(AbortSignal))
    await act(async () => { await vi.advanceTimersByTimeAsync(4_000) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    frameReady = true; mediaTime = 10
    await act(async () => { released({ lease_id: 'late-original-lease', status: 'released' }) })
    await act(async () => { await vi.advanceTimersByTimeAsync(2_100) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
    expect(queryCameraDevices('synthetic-session', 'camera').devices[0]).toMatchObject({ track_live: true, frame_confirmed: true })
  })

  it('aborts hung qualification and releases the actual lease before retry, ignoring its late result', async () => {
    let qualified!: (value: never) => void
    vi.mocked(localApi.startObservationQualification).mockReturnValueOnce(new Promise(resolve => { qualified = resolve }))
    automatic()
    await act(async () => { await vi.advanceTimersByTimeAsync(12_100) })
    expect(stops[0]).toHaveBeenCalledTimes(1)
    expect(localApi.releaseDeviceLease).toHaveBeenCalledWith('camera-lease', 'camera_startup_api_timeout', expect.any(AbortSignal))
    const originalSignal = (vi.mocked(localApi.startObservationQualification).mock.calls[0] as [string, AbortSignal])[1]
    expect(originalSignal?.aborted).toBe(true)
    await act(async () => { qualified({} as never) })
    expect(queryCameraDevices('synthetic-session', 'camera').devices[0]).toMatchObject({ track_live: false, frame_confirmed: false })
    frameReady = true; mediaTime = 10
    await act(async () => { await vi.advanceTimersByTimeAsync(2_100) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
    expect(queryCameraDevices('synthetic-session', 'camera').devices[0]).toMatchObject({ track_live: true, frame_confirmed: true })
  })

  it('retires a hung coverage start and closes its original late epoch before any replacement epoch', async () => {
    automatic()
    let begun!: (value: { status: string; event_id: string }) => void
    let ended!: (value: { status: string; event_id: string }) => void
    vi.mocked(localApi.beginObservationStream).mockReturnValueOnce(new Promise(resolve => { begun = resolve }))
    vi.mocked(localApi.endObservationStream).mockReturnValueOnce(new Promise(resolve => { ended = resolve }))
    await act(async () => { await vi.advanceTimersByTimeAsync(12_100) })
    const originalEpoch = vi.mocked(localApi.beginObservationStream).mock.calls[0][1]
    expect(stops[0]).toHaveBeenCalledTimes(1)
    expect(localApi.releaseDeviceLease).toHaveBeenCalledWith('camera-lease', 'camera_startup_api_timeout', expect.any(AbortSignal))
    await act(async () => { await vi.advanceTimersByTimeAsync(40_000) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    expect(localApi.beginObservationStream).toHaveBeenCalledTimes(1)
    expect(localApi.endObservationStream).not.toHaveBeenCalled()
    await act(async () => { begun(coverageReceipt(originalEpoch, 'start')) })
    expect(localApi.endObservationStream).toHaveBeenCalledWith('synthetic-session', originalEpoch, 0, 'unavailable', expect.any(AbortSignal))
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    frameReady = true; mediaTime = 10
    await act(async () => { ended(coverageReceipt(originalEpoch, 'stop')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(2_100) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
    expect(vi.mocked(localApi.beginObservationStream).mock.calls[1][1]).not.toBe(originalEpoch)
  })

  it('does not admit explicit resume while a known old lease release is still pending', async () => {
    frameReady = true
    let released!: (value: { lease_id: string; status: string }) => void
    vi.mocked(localApi.releaseDeviceLease).mockReturnValueOnce(new Promise(resolve => { released = resolve }))
    automatic()
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    await act(async () => { await vi.advanceTimersByTimeAsync(6_100) })
    expect(stops[0]).toHaveBeenCalledTimes(1)
    await act(async () => { window.dispatchEvent(new Event('hsc-camera-observation-resume')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    mediaTime = 10
    await act(async () => { released({ lease_id: 'camera-lease', status: 'released' }) })
    await act(async () => { await vi.advanceTimersByTimeAsync(2_100) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
  })

  it('quarantines an accepted lease across workspace replacement until its exact release acknowledgement', async () => {
    frameReady = true
    let released!: (value: { lease_id: string; status: string }) => void
    vi.mocked(localApi.releaseDeviceLease).mockReturnValueOnce(new Promise(resolve => { released = resolve }))
    const first = automatic('old-synthetic-session')
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    await act(async () => { window.dispatchEvent(new Event('hsc-privacy-pause')) })
    first.unmount()
    automatic('new-synthetic-session')
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    expect(stops[0]).toHaveBeenCalledTimes(1)
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    mediaTime = 10
    await act(async () => { released({ lease_id: 'camera-lease', status: 'released' }) })
    await act(async () => { await vi.advanceTimersByTimeAsync(2_100) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
    expect(vi.mocked(localApi.registerDevice).mock.calls[1][0]).toBe('new-synthetic-session')
  })

  it.each(['hung stop', 'foreign epoch receipt'] as const)('retains the original coverage after %s and retries only that epoch with its first closure', async failure => {
    frameReady = true
    if (failure === 'hung stop') vi.mocked(localApi.endObservationStream).mockReturnValueOnce(new Promise(() => {}))
    else vi.mocked(localApi.endObservationStream).mockResolvedValueOnce({ status: 'recorded', event_id: 'coverage-action:foreign-epoch' })
    automatic()
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    const originalEpoch = vi.mocked(localApi.beginObservationStream).mock.calls[0][1]
    await act(async () => { window.dispatchEvent(new Event('hsc-privacy-pause')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(5_100) })
    if (failure === 'hung stop') expect((vi.mocked(localApi.endObservationStream).mock.calls[0] as [string, string, number, string, AbortSignal])[4].aborted).toBe(true)
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    expect(queryCameraDevices('synthetic-session', 'camera').devices[0].track_live).toBe(false)
    mediaTime = 10
    await act(async () => { window.dispatchEvent(new Event('hsc-camera-observation-resume')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(2_100) })
    expect(localApi.endObservationStream).toHaveBeenNthCalledWith(2, 'synthetic-session', originalEpoch, 0, 'paused', expect.any(AbortSignal))
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
    expect(vi.mocked(localApi.beginObservationStream).mock.calls[1][1]).not.toBe(originalEpoch)
  })

  it.each(['hung begin', 'hung stop'] as const)('quarantines the same physical camera across another session remount while original coverage has a %s', async failure => {
    frameReady = true
    let begun: ((value: { status: string; event_id: string }) => void) | undefined
    let ended: ((value: { status: string; event_id: string }) => void) | undefined
    if (failure === 'hung begin') vi.mocked(localApi.beginObservationStream).mockReturnValueOnce(new Promise(resolve => { begun = resolve }))
    vi.mocked(localApi.endObservationStream).mockReturnValueOnce(new Promise(resolve => { ended = resolve }))
    const first = automatic('old-synthetic-session')
    await act(async () => { await vi.advanceTimersByTimeAsync(failure === 'hung begin' ? 12_100 : 100) })
    const originalEpoch = vi.mocked(localApi.beginObservationStream).mock.calls[0][1]
    recoverOriginalCoverage = () => {
      begun?.(coverageReceipt(originalEpoch, 'start'))
      ended?.(coverageReceipt(originalEpoch, 'stop'))
    }
    if (failure === 'hung stop') await act(async () => { window.dispatchEvent(new Event('hsc-privacy-pause')) })
    first.unmount()
    automatic('new-synthetic-session')
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    expect(stops[0]).toHaveBeenCalledTimes(1)
    expect(localApi.releaseDeviceLease).toHaveBeenCalledWith('camera-lease', failure === 'hung begin' ? 'camera_startup_api_timeout' : 'voice_privacy_pause', expect.any(AbortSignal))
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    expect(localApi.beginObservationStream).toHaveBeenCalledTimes(1)
    if (failure === 'hung begin') await act(async () => { begun!(coverageReceipt(originalEpoch, 'start')) })
    expect(localApi.endObservationStream).toHaveBeenCalledWith('old-synthetic-session', originalEpoch, 0, failure === 'hung begin' ? 'unavailable' : 'paused', expect.any(AbortSignal))
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    mediaTime = 10
    await act(async () => { ended!(coverageReceipt(originalEpoch, 'stop')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(2_100) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
    expect(vi.mocked(localApi.beginObservationStream).mock.calls[1][0]).toBe('new-synthetic-session')
    expect(vi.mocked(localApi.beginObservationStream).mock.calls[1][1]).not.toBe(originalEpoch)
  })

  it('quarantines a pending registration across privacy pause and workspace replacement until its original release', async () => {
    let registered!: (lease: { lease_id: string; status: string; expires_at: string }) => void
    let released!: (lease: { lease_id: string; status: string }) => void
    vi.mocked(localApi.registerDevice).mockReturnValueOnce(new Promise(resolve => { registered = resolve }))
    vi.mocked(localApi.releaseDeviceLease).mockReturnValueOnce(new Promise(resolve => { released = resolve }))
    const first = automatic('old-synthetic-session')
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    await act(async () => { window.dispatchEvent(new Event('hsc-privacy-pause')) })
    first.unmount()
    automatic('new-synthetic-session')
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000) })
    expect(stops[0]).toHaveBeenCalledTimes(1)
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    await act(async () => { registered({ lease_id: 'retired-original-lease', status: 'active', expires_at: 'later' }) })
    expect(localApi.releaseDeviceLease).toHaveBeenCalledWith('retired-original-lease', 'observation_registration_retired', expect.any(AbortSignal))
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    frameReady = true; mediaTime = 10
    await act(async () => { released({ lease_id: 'retired-original-lease', status: 'released' }) })
    await act(async () => { await vi.advanceTimersByTimeAsync(2_100) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
    expect(vi.mocked(localApi.registerDevice).mock.calls[1][0]).toBe('new-synthetic-session')
  })

  it.each(['hung release', 'wrong lease receipt'] as const)('keeps a late registration closed after %s and explicit retry releases the exact original lease', async failure => {
    let registered!: (lease: { lease_id: string; status: string; expires_at: string }) => void
    vi.mocked(localApi.registerDevice).mockReturnValueOnce(new Promise(resolve => { registered = resolve }))
    vi.mocked(localApi.releaseDeviceLease).mockImplementation(async leaseId => ({ lease_id: leaseId, status: 'released' }))
    if (failure === 'hung release') vi.mocked(localApi.releaseDeviceLease).mockReturnValueOnce(new Promise(() => {}))
    else vi.mocked(localApi.releaseDeviceLease).mockResolvedValueOnce({ lease_id: 'different-owner-lease', status: 'released' })
    automatic()
    await act(async () => { await vi.advanceTimersByTimeAsync(12_100) })
    await act(async () => { registered({ lease_id: 'exact-retired-lease', status: 'active', expires_at: 'later' }) })
    await act(async () => { await vi.advanceTimersByTimeAsync(5_100) })
    if (failure === 'hung release') expect((vi.mocked(localApi.releaseDeviceLease).mock.calls[0] as [string, string, AbortSignal])[2].aborted).toBe(true)
    await act(async () => { await vi.advanceTimersByTimeAsync(35_000) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    expect(queryCameraDevices('synthetic-session', 'camera').devices[0].track_live).toBe(false)
    frameReady = true; mediaTime = 10
    await act(async () => { window.dispatchEvent(new Event('hsc-camera-observation-resume')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(2_100) })
    expect(localApi.releaseDeviceLease).toHaveBeenNthCalledWith(2, 'exact-retired-lease', 'observation_registration_retired', expect.any(AbortSignal))
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
  })

  it('bounds a hung camera opener without overlapping retries and stops its late stream', async () => {
    let resolve!: (stream: MediaStream) => void
    vi.mocked(navigator.mediaDevices.getUserMedia).mockImplementationOnce(() => new Promise(value => { resolve = value }))
    automatic()
    await act(async () => { await vi.advanceTimersByTimeAsync(20_100) })
    expect(localApi.recordDeviceIncident).toHaveBeenCalledWith('synthetic-session', 'camera', 'DEVICE_START_TIMEOUT', expect.any(String))
    expect(localApi.registerDevice).not.toHaveBeenCalled()
    await act(async () => { await vi.advanceTimersByTimeAsync(90_000) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    const stop = vi.fn()
    frameReady = true
    await act(async () => { resolve({ getTracks: () => [{ stop }] } as unknown as MediaStream) })
    expect(stop).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(30_100) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
    expect(localApi.registerDevice).toHaveBeenCalledTimes(1)
    mediaTime = 10 // A new frame on the replacement stream, not the late one.
    expect(queryCameraDevices('synthetic-session', 'camera').devices[0]).toMatchObject({ track_live: true, frame_confirmed: true })
  })

  it('keeps a timed-out opener retired after privacy pause when its stream finally arrives', async () => {
    let resolve!: (stream: MediaStream) => void
    vi.mocked(navigator.mediaDevices.getUserMedia).mockImplementationOnce(() => new Promise(value => { resolve = value }))
    automatic()
    await act(async () => { await vi.advanceTimersByTimeAsync(20_100) })
    expect(localApi.recordDeviceIncident).toHaveBeenCalledWith('synthetic-session', 'camera', 'DEVICE_START_TIMEOUT', expect.any(String))
    await act(async () => { window.dispatchEvent(new Event('hsc-privacy-pause')) })
    const stop = vi.fn()
    await act(async () => { resolve({ getTracks: () => [{ stop }] } as unknown as MediaStream) })
    await act(async () => { await vi.advanceTimersByTimeAsync(90_000) })
    expect(stop).toHaveBeenCalledTimes(1)
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    expect(localApi.registerDevice).not.toHaveBeenCalled()
  })

  it('quarantines the same bound device across workspace replacement until the old request settles', async () => {
    let resolve!: (stream: MediaStream) => void
    vi.mocked(navigator.mediaDevices.getUserMedia).mockImplementationOnce(() => new Promise(value => { resolve = value }))
    const first = automatic('old-synthetic-session')
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    first.unmount()
    automatic('new-synthetic-session')
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    expect(localApi.registerDevice).not.toHaveBeenCalled()
    const stop = vi.fn()
    frameReady = true
    await act(async () => { resolve({ getTracks: () => [{ stop }] } as unknown as MediaStream) })
    expect(stop).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(2_100) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
    expect(localApi.registerDevice).toHaveBeenCalledExactlyOnceWith('new-synthetic-session', 'camera', 'camera-hash', {})
  })

  it('does not let late stall cleanup reopen a camera after a privacy pause', async () => {
    frameReady = true
    let saved!: (value: never) => void
    vi.mocked(localApi.releaseDeviceLease).mockReturnValueOnce(new Promise((resolve) => { saved = resolve }))
    automatic()
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    await act(async () => { await vi.advanceTimersByTimeAsync(6_100) })
    expect(stops[0]).toHaveBeenCalledTimes(1)
    await act(async () => { window.dispatchEvent(new Event('hsc-privacy-pause')) })
    await act(async () => { saved({ lease_id: 'camera-lease', status: 'released' } as never) })
    await act(async () => { await vi.advanceTimersByTimeAsync(35_000) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    expect(queryCameraDevices('synthetic-session', 'camera').devices[0]).toMatchObject({ track_live: false, frame_confirmed: false })
  })

  it('keeps a stalled camera closed when lease release is unconfirmed', async () => {
    frameReady = true
    vi.mocked(localApi.releaseDeviceLease).mockRejectedValue(new Error('lease service unavailable'))
    automatic()
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    await act(async () => { await vi.advanceTimersByTimeAsync(40_000) })
    expect(stops[0]).toHaveBeenCalledTimes(1)
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
  })

  it('does not reopen on a first-frame cleanup receipt arriving after privacy pause', async () => {
    let saved!: (value: never) => void
    vi.mocked(localApi.releaseDeviceLease).mockReturnValueOnce(new Promise((resolve) => { saved = resolve }))
    automatic()
    await act(async () => { await vi.advanceTimersByTimeAsync(12_200) })
    expect(stops[0]).toHaveBeenCalledTimes(1)
    await act(async () => { window.dispatchEvent(new Event('hsc-privacy-pause')) })
    await act(async () => { saved({ lease_id: 'camera-lease', status: 'released' } as never) })
    await act(async () => { await vi.advanceTimersByTimeAsync(35_000) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
  })

  it('ignores a late lease-renewal failure after the child pauses observation', async () => {
    frameReady = true
    let reject!: (error: Error) => void
    vi.spyOn(localApi, 'renewDeviceLease').mockReturnValueOnce(new Promise((_, fail) => { reject = fail }))
    automatic()
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    for (let second = 1; second <= 61; second++) {
      mediaTime = second
      await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    }
    expect(localApi.renewDeviceLease).toHaveBeenCalledTimes(1)
    await act(async () => { window.dispatchEvent(new Event('hsc-privacy-pause')) })
    await act(async () => { reject(new Error('late renewal failure')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(35_000) })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    expect(queryCameraDevices('synthetic-session', 'camera').devices[0].track_live).toBe(false)
  })

  it('does not complete before a live frame and keeps that stream through persisted-prop refresh', async () => {
    const view = mount()
    let finished = false
    const result = requestCameraDevice('synthetic-session', 'camera', 'resume').then((value) => { finished = true; return value })
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    expect(localApi.resumeObservation).toHaveBeenCalledTimes(1)
    expect(finished).toBe(false)
    expect(queryCameraDevices('synthetic-session', 'camera').devices[0]).toMatchObject({ track_live: true, frame_confirmed: false })
    frameReady = true
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    expect(await result).toEqual({ device_type: 'camera', device_status: 'active', frame_confirmed: true, lease_confirmed: true, observation_status: 'enabled' })
    view.rerender(<LocalObservation sessionId="synthetic-session" active enabled snoozeUntil={null} />)
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    expect(stops[0]).not.toHaveBeenCalled()
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledWith({ audio: false, video: expect.objectContaining({ deviceId: { exact: 'bound-camera' } }) })
  })

  it('releases the track immediately but waits for lease and persistence before paused', async () => {
    frameReady = true; mount()
    const opened = requestCameraDevice('synthetic-session', 'camera', 'resume')
    await act(async () => { await vi.advanceTimersByTimeAsync(100) }); await opened
    let release!: (value: never) => void
    vi.mocked(localApi.releaseDeviceLease).mockReturnValueOnce(new Promise((resolve) => { release = resolve }))
    let finished = false
    const paused = requestCameraDevice('synthetic-session', 'camera', 'pause').then((value) => { finished = true; return value })
    expect(stops[0]).toHaveBeenCalledTimes(1)
    await act(async () => { await Promise.resolve() })
    expect(finished).toBe(false)
    await act(async () => { release({ lease_id: 'camera-lease', status: 'released' } as never) })
    expect(await paused).toMatchObject({ device_status: 'paused', frame_confirmed: false })
    expect(queryCameraDevices('synthetic-session', 'camera').devices[0].track_live).toBe(false)
  })

  it('fails closed for no frames and never confuses another session or document camera', async () => {
    mount()
    const pending = requestCameraDevice('synthetic-session', 'camera', 'resume').catch((error) => error)
    await act(async () => { await vi.advanceTimersByTimeAsync(5_200) })
    expect(await pending).toBeInstanceOf(Error)
    expect(stops[0]).toHaveBeenCalled()
    await expect(requestCameraDevice('other-session', 'camera', 'resume')).rejects.toThrow('DEVICE_RECEIVER_UNAVAILABLE')
    expect(queryCameraDevices('synthetic-session', 'document_camera').devices[0]).toMatchObject({ track_live: false, frame_confirmed: false })
    expect(localApi.resumeObservation).toHaveBeenCalledTimes(1)
  })

  it.each(['cancel', 'unmount'])('does not open after a late saved resume following %s', async (boundary) => {
    let save!: (value: never) => void
    vi.mocked(localApi.resumeObservation).mockReturnValueOnce(new Promise((resolve) => { save = resolve }))
    const view = mount(); const controller = new AbortController()
    const pending = requestCameraDevice('synthetic-session', 'camera', 'resume', controller.signal).catch((error) => error)
    await act(async () => { if (boundary === 'cancel') controller.abort(); else view.unmount() })
    await act(async () => { save({ observation_status: 'enabled' } as never) })
    expect(await pending).toMatchObject({ name: boundary === 'cancel' ? 'AbortError' : 'Error' })
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled()
  })

  it('does not report paused when lease release is rejected', async () => {
    frameReady = true; mount()
    const opened = requestCameraDevice('synthetic-session', 'camera', 'resume')
    await act(async () => { await vi.advanceTimersByTimeAsync(100) }); await opened
    vi.mocked(localApi.releaseDeviceLease).mockRejectedValue(new Error('lease offline'))
    const paused = requestCameraDevice('synthetic-session', 'camera', 'pause').catch((error) => error)
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    expect(await paused).toBeInstanceOf(Error)
    expect(stops[0]).toHaveBeenCalled()
  })
})

describe('camera media clock health', () => {
  it.each(['camera', 'document_camera'] as const)('detects a stalled %s after the system clock moves backwards', (device) => {
    vi.useFakeTimers()
    try {
      const stream = { getVideoTracks: () => [{ readyState: 'live', enabled: true, muted: false }] } as unknown as MediaStream
      const video = { srcObject: stream, readyState: 2, videoWidth: 1280, videoHeight: 720,
        currentTime: 1, getVideoPlaybackQuality: () => ({ totalVideoFrames: 12 }) } as HTMLVideoElement
      const clock = new CameraFrameClock()
      const first = clock.read(device, video, stream, true, null)
      vi.setSystemTime(Date.now() - 3_600_000)
      vi.advanceTimersByTime(5_000)
      expect(clock.read(device, video, stream, true, null)).toMatchObject({
        frame_confirmed: false, monitor_active: false, last_frame_at: first.last_frame_at,
      })
    } finally { vi.useRealTimers() }
  })
  it.each(['camera', 'document_camera'] as const)('does not falsely retire %s when only the system clock jumps forwards', (device) => {
    vi.useFakeTimers()
    try {
      const stream = { getVideoTracks: () => [{ readyState: 'live', enabled: true, muted: false }] } as unknown as MediaStream
      const video = { srcObject: stream, readyState: 2, videoWidth: 1280, videoHeight: 720,
        currentTime: 1, getVideoPlaybackQuality: () => ({ totalVideoFrames: 12 }) } as HTMLVideoElement
      const clock = new CameraFrameClock()
      const first = clock.read(device, video, stream, true, null)
      vi.setSystemTime(Date.now() + 3_600_000)
      expect(clock.read(device, video, stream, true, null)).toMatchObject({
        frame_confirmed: true, monitor_active: true, last_frame_at: first.last_frame_at,
      })
    } finally { vi.useRealTimers() }
  })
  it('keeps the first-frame wait bounded when the system clock moves backwards', async () => {
    vi.useFakeTimers()
    const controller = new AbortController()
    let pending: Promise<void> | undefined
    try {
      let result: Error | undefined
      pending = waitForCameraFrame(() => ({ video: null, stream: null, leased: false, generation: 0 }), controller.signal)
        .catch(error => { result = error as Error })
      await vi.advanceTimersByTimeAsync(1_000)
      vi.setSystemTime(Date.now() - 3_600_000)
      await vi.advanceTimersByTimeAsync(4_000)
      expect(result?.message).toBe('DEVICE_NO_CONFIRMED_FRAME')
      await pending
    } finally { controller.abort(); await pending; vi.useRealTimers() }
  })
  it.each(['camera', 'document_camera'] as const)('keeps %s healthy when offscreen frames arrive without being displayed', (device) => {
    vi.useFakeTimers()
    try {
      const stream = { getVideoTracks: () => [{ readyState: 'live', enabled: true, muted: false }] } as unknown as MediaStream
      let frames = 270
      let dropped = 4
      const video = {
        srcObject: stream, readyState: 2, videoWidth: 1280, videoHeight: 720, currentTime: 1,
        getVideoPlaybackQuality: () => ({ totalVideoFrames: frames, droppedVideoFrames: dropped }),
      } as HTMLVideoElement
      const clock = new CameraFrameClock()
      expect(clock.read(device, video, stream, true, null).frame_confirmed).toBe(true)
      for (let second = 1; second <= 8; second++) {
        vi.advanceTimersByTime(1_000)
        frames += 8; dropped += 8
        expect(clock.read(device, video, stream, true, null).monitor_active).toBe(true)
      }
      // Dropping an already received frame does not prove a new source frame.
      for (let second = 1; second <= 6; second++) {
        vi.advanceTimersByTime(1_000)
        dropped++
        clock.read(device, video, stream, true, null)
      }
      expect(clock.read(device, video, stream, true, null).frame_confirmed).toBe(false)
    } finally { vi.useRealTimers() }
  })
  it('detects stopped decoded frames even if the media clock keeps advancing', () => {
    vi.useFakeTimers()
    try {
      const stream = { getVideoTracks: () => [{ readyState: 'live', enabled: true, muted: false }] } as unknown as MediaStream
      let frames = 12
      let mediaTime = 1
      const video = {
        srcObject: stream, readyState: 2, videoWidth: 1280, videoHeight: 720,
        get currentTime() { return mediaTime },
        getVideoPlaybackQuality: () => ({ totalVideoFrames: frames }),
      } as HTMLVideoElement
      const clock = new CameraFrameClock()
      expect(clock.read('camera', video, stream, true, null).frame_confirmed).toBe(true)
      for (let second = 1; second <= 6; second++) {
        mediaTime++
        vi.advanceTimersByTime(1_000)
        clock.read('camera', video, stream, true, null)
      }
      expect(clock.read('camera', video, stream, true, null).frame_confirmed).toBe(false)
      // A motionless scene is healthy when real frames continue to decode.
      frames++
      expect(clock.read('camera', video, stream, true, null).frame_confirmed).toBe(true)
    } finally { vi.useRealTimers() }
  })
  it('uses playback progress rather than scene pixel movement to detect a stalled stream', () => {
    vi.useFakeTimers()
    const track = { readyState: 'live', enabled: true, muted: false } as MediaStreamTrack
    const stream = { getVideoTracks: () => [track] } as unknown as MediaStream
    let mediaTime = 1
    const video = {
      srcObject: stream, readyState: 2, videoWidth: 1280, videoHeight: 720,
      get currentTime() { return mediaTime },
    } as HTMLVideoElement
    const clock = new CameraFrameClock()
    expect(clock.read('camera', video, stream, true, null).frame_confirmed).toBe(true)
    vi.advanceTimersByTime(5_100)
    expect(clock.read('camera', video, stream, true, null).frame_confirmed).toBe(false)
    mediaTime = 2
    expect(clock.read('camera', video, stream, true, null).frame_confirmed).toBe(true)
    vi.useRealTimers()
  })
})

describe('paused and ended frame boundaries', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  const fixture = () => {
    const track = { readyState: 'live', enabled: true, muted: false }
    const stream = { getVideoTracks: () => [track] } as unknown as MediaStream
    const video = { srcObject: stream, readyState: 2, videoWidth: 1280, videoHeight: 720,
      currentTime: 1, paused: false, ended: false, frames: 12,
      getVideoPlaybackQuality() { return { totalVideoFrames: this.frames, droppedVideoFrames: this.frames - 12 } },
    }
    return { stream, video, element: video as unknown as HTMLVideoElement }
  }

  for (const device of ['camera', 'document_camera'] as const) {
    it.each(['paused', 'ended'] as const)('keeps the last confirmed frame of ' + device + ' when %s despite advancing counters', (state) => {
      const f = fixture(), clock = new CameraFrameClock()
      const before = clock.read(device, f.element, f.stream, true, null)
      expect(before.frame_confirmed).toBe(true)
      f.video[state] = true
      for (let second = 1; second <= 6; second++) {
        f.video.frames += 8; f.video.currentTime++
        vi.advanceTimersByTime(1_000)
        const result = clock.read(device, f.element, f.stream, true, null)
        expect(result).toMatchObject({ track_live: true, frame_confirmed: false,
          monitor_active: false, last_frame_at: before.last_frame_at })
        // This is the existing product watchdog's unchanged five-second gate.
        const stalled = !result.frame_confirmed && result.last_frame_at !== null
          && Date.now() - Date.parse(result.last_frame_at) >= 5_000
        expect(stalled).toBe(second >= 5)
      }
    })

    it('accepts advancing ' + device + ' frames after playback resumes', () => {
      const f = fixture(), clock = new CameraFrameClock()
      const before = clock.read(device, f.element, f.stream, true, null)
      f.video.paused = true
      vi.advanceTimersByTime(6_000)
      expect(clock.read(device, f.element, f.stream, true, null)).toMatchObject({
        frame_confirmed: false, last_frame_at: before.last_frame_at })
      f.video.paused = false; f.video.frames++; f.video.currentTime++
      expect(clock.read(device, f.element, f.stream, true, null)).toMatchObject({
        track_live: true, frame_confirmed: true, monitor_active: true,
        last_frame_at: new Date(Date.now()).toISOString() })
    })
  }

  it.each(['paused', 'ended'] as const)('never completes the first-frame wait for a %s preview', async (state) => {
    const f = fixture(); f.video[state] = true
    let settled = false
    const pending = waitForCameraFrame(() => ({ video: f.element, stream: f.stream, leased: true, generation: 0 }), new AbortController().signal)
      .then(() => { settled = true; return null }, error => { settled = true; return error as Error })
    await vi.advanceTimersByTimeAsync(4_999)
    expect(settled).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(await pending).toMatchObject({ message: 'DEVICE_NO_CONFIRMED_FRAME' })
  })

  it('completes the original first-frame wait only after playback resumes', async () => {
    const f = fixture(); f.video.paused = true
    let settled = false
    const pending = waitForCameraFrame(() => ({ video: f.element, stream: f.stream, leased: true, generation: 0 }), new AbortController().signal)
      .then(() => { settled = true })
    await vi.advanceTimersByTimeAsync(200)
    expect(settled).toBe(false)
    f.video.paused = false; f.video.frames++
    await vi.advanceTimersByTimeAsync(50)
    expect(settled).toBe(false) // Resume alone exposes only the cached baseline.
    f.video.frames++
    await vi.advanceTimersByTimeAsync(50)
    await pending
    expect(settled).toBe(true)
  })

  it('preserves cancellation while the paused first-frame wait is pending', async () => {
    const f = fixture(); f.video.paused = true
    const controller = new AbortController()
    const pending = waitForCameraFrame(() => ({ video: f.element, stream: f.stream, leased: true, generation: 0 }), controller.signal)
      .then(() => null, error => error as Error)
    await vi.advanceTimersByTimeAsync(100)
    controller.abort()
    expect(await pending).toMatchObject({ name: 'AbortError' })
  })
})
