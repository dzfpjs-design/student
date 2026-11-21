import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, localApi } from './api'
import { ContinuousDocumentMonitor } from './ContinuousDocumentMonitor'
import { DocumentCameraCapture } from './DocumentCameraCapture'
import * as frameMonitor from './documentFrameMonitor'
import { configureDocumentCameraAutofocus } from './mediaDevices'
import { queryCameraDevices, requestCameraDevice } from './cameraDeviceRuntime'
import { requestOwnedVoiceEvent } from './voiceJourneyRuntime'
import { clearVoiceCaptureContext, publishVoiceDocumentCapture, voiceCaptureReference } from './voiceCaptureContext'
import { mistakeLearningApi } from './mistakeLearningApi'

// A successful play() must model the playing state as well as its fulfilled promise.
function mockSuccessfulMediaPlay(this: HTMLMediaElement): Promise<void> {
  Object.defineProperties(this, {
    paused: { configurable: true, value: false },
    ended: { configurable: true, value: false },
    requestVideoFrameCallback: { configurable: true, value: (fn: VideoFrameRequestCallback) => window.setTimeout(() => fn(performance.now(), { presentedFrames: performance.now(), mediaTime: performance.now() / 1000 } as VideoFrameCallbackMetadata), 25) },
    cancelVideoFrameCallback: { configurable: true, value: (id: number) => window.clearTimeout(id) },
  })
  return Promise.resolve()
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function mockUsableVideoFrame(): void {
  vi.spyOn(HTMLMediaElement.prototype, 'readyState', 'get').mockReturnValue(4)
  vi.spyOn(HTMLVideoElement.prototype, 'videoWidth', 'get').mockReturnValue(1280)
  vi.spyOn(HTMLVideoElement.prototype, 'videoHeight', 'get').mockReturnValue(720)
}

vi.mock('./mediaDevices', () => ({
  configureDocumentCameraAutofocus: vi.fn().mockResolvedValue({ exposed: true, applied: true, requested_mode: 'continuous', active_mode: 'continuous' }),
  resolveBoundVideoDevice: vi.fn().mockResolvedValue({ device: { deviceId: 'document-camera', kind: 'videoinput', label: 'Fic760', groupId: 'group' }, method: 'system_id_hash' }),
  documentCameraVideoConstraints: vi.fn((deviceId: string) => ({ deviceId: { exact: deviceId }, width: { ideal: 1280, max: 1280 }, height: { ideal: 720, max: 720 }, frameRate: { ideal: 10, max: 15 } })),
  openDocumentCameraStream: vi.fn((deviceId: string) => navigator.mediaDevices.getUserMedia({
    audio: false,
    video: { deviceId: { exact: deviceId }, width: { ideal: 1280, max: 1280 }, height: { ideal: 720, max: 720 }, frameRate: { ideal: 10, max: 15 } },
  })),
  mediaDeviceRegistration: vi.fn().mockResolvedValue({ systemIdHash: 'a'.repeat(64), capabilities: { width: 2560, height: 1440 } }),
  mediaDeviceFailure: vi.fn().mockReturnValue({ code: 'DEVICE_ERROR', message: '设备不可用。' })
}))

describe('continuous document monitor device lifecycle', () => {
  const mediaListeners = new Map<string, () => void>()
  const trackListeners = new Map<string, () => void>()
  const stop = vi.fn()
  const track = { readyState: 'live', enabled: true, muted: false, stop, addEventListener: vi.fn((name: string, listener: () => void) => trackListeners.set(name, listener)) }
  const stream = { getTracks: () => [track], getVideoTracks: () => [track] } as unknown as MediaStream

  beforeEach(() => {
    mediaListeners.clear()
    trackListeners.clear()
    stop.mockClear()
    track.enabled = true; track.muted = false
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      enumerateDevices: vi.fn().mockResolvedValue([]),
      getUserMedia: vi.fn().mockResolvedValue(stream),
      addEventListener: vi.fn((name: string, listener: () => void) => mediaListeners.set(name, listener)),
      removeEventListener: vi.fn((name: string) => mediaListeners.delete(name)),
    } })
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    vi.spyOn(localApi, 'documentMonitorStatus').mockResolvedValue({ session_id: 'session', status: 'not_started', latest_event: null, latest_revision: null })
    vi.spyOn(localApi, 'agentRuntimeStatus').mockResolvedValue({ document_guidance_preferences: { child_id: 'child', occlusion_reminders_enabled: true, settings_saved: false, source: 'default' } } as never)
    vi.spyOn(localApi, 'startDocumentMonitor').mockResolvedValue({ session_id: 'session', status: 'active', latest_event: null, latest_revision: null })
    vi.spyOn(localApi, 'pendingDocumentReceipts').mockResolvedValue([])
    vi.spyOn(localApi, 'acknowledgeDocumentReceipt').mockImplementation(async (session, operation, action) => ({
      session_id: session, client_action_id: action, operation, status: 'saved', local_only: true, replay_allowed: false,
      artifact_id: 'old-page', revision_id: operation === 'ocr' ? null : 'old-version'
    }))
    vi.spyOn(localApi, 'pauseDocumentMonitor').mockResolvedValue({ session_id: 'session', status: 'paused', latest_event: null, latest_revision: null })
    vi.spyOn(localApi, 'resumeDocumentMonitor').mockResolvedValue({ session_id: 'session', status: 'active', latest_event: null, latest_revision: null })
    vi.spyOn(localApi, 'deviceBindings').mockResolvedValue([{
      device_type: 'document_camera', system_id_hash: 'a'.repeat(64), label: 'Fic760',
      capabilities: { width: 2560, height: 1440 }, verified_at: new Date().toISOString(), updated_at: new Date().toISOString()
    }])
    vi.spyOn(localApi, 'confirmDeviceFrame').mockImplementation(async lease => ({ lease_id: lease, status: 'frame_confirmed', resolved_incidents: 0 }))
    vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'lease-document', status: 'active', expires_at: new Date().toISOString() })
    vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'lease-document', status: 'released' })
    vi.spyOn(localApi, 'renewDeviceLease').mockResolvedValue({ lease_id: 'lease-document', status: 'active', expires_at: new Date().toISOString() })
  })

  afterEach(() => {
    cleanup()
    clearVoiceCaptureContext()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('automatically opens for an active task and yields before manual capture', async () => {
    mockUsableVideoFrame()
    const view = render(<ContinuousDocumentMonitor sessionId="session" active />)
    await waitFor(() => expect(localApi.registerDevice).toHaveBeenCalledWith('session', 'document_camera', 'a'.repeat(64), expect.any(Object)))
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledWith({
      audio: false,
      video: {
        deviceId: { exact: 'document-camera' },
        width: { ideal: 1280, max: 1280 },
        height: { ideal: 720, max: 720 },
        frameRate: { ideal: 10, max: 15 },
      },
    })
    expect(view.getByText(/持续本地监控已开启/)).toBeTruthy()
    expect(view.getByText('多帧遮挡补全暂不可用，当前使用单帧识别。')).toBeTruthy()

    view.rerender(<ContinuousDocumentMonitor sessionId="session" active={false} />)
    await waitFor(() => expect(localApi.releaseDeviceLease).toHaveBeenCalledWith('lease-document', 'document_monitor_yielded', expect.any(AbortSignal)))
    expect(stop).toHaveBeenCalled()
  })

  it('yields without saving a pause when the workspace is temporarily inactive', async () => {
    mockUsableVideoFrame()
    const view = render(<ContinuousDocumentMonitor sessionId="session" active={false} />)
    expect(localApi.pauseDocumentMonitor).not.toHaveBeenCalled()
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled()
    view.rerender(<ContinuousDocumentMonitor sessionId="session" active />)
    await view.findByText('实时画面')
    view.rerender(<ContinuousDocumentMonitor sessionId="session" active={false} />)
    expect(stop).toHaveBeenCalledOnce()
    view.rerender(<ContinuousDocumentMonitor sessionId="session" active />)
    await waitFor(() => expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2))
    expect(localApi.pauseDocumentMonitor).not.toHaveBeenCalled()
  })

  it('saves an explicit pause after a pending start and restores it until the learner resumes', async () => {
    mockUsableVideoFrame()
    type Monitor = Awaited<ReturnType<typeof localApi.startDocumentMonitor>>
    const starting = deferred<Monitor>()
    const activeMonitor = { session_id: 'session', status: 'active', latest_event: null, latest_revision: null } as const
    vi.mocked(localApi.startDocumentMonitor).mockReturnValueOnce(starting.promise)
    const first = render(<ContinuousDocumentMonitor sessionId="session" active />)
    await waitFor(() => expect(localApi.startDocumentMonitor).toHaveBeenCalledOnce())
    fireEvent.click(first.getByRole('button', { name: '暂停高拍仪' }))
    expect(first.getByRole('button', { name: '继续高拍仪' })).toBeTruthy()
    expect(localApi.pauseDocumentMonitor).not.toHaveBeenCalled()
    await act(async () => { starting.resolve(activeMonitor) })
    await waitFor(() => expect(localApi.pauseDocumentMonitor).toHaveBeenCalledExactlyOnceWith('session'))
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled()
    first.unmount()

    vi.mocked(localApi.documentMonitorStatus).mockResolvedValueOnce({ ...activeMonitor, status: 'paused' })
    const second = render(<ContinuousDocumentMonitor sessionId="session" active />)
    await second.findByText('高拍仪监控已暂停。')
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled()
    fireEvent.click(second.getByRole('button', { name: '继续高拍仪' }))
    await second.findByText('实时画面')
    expect(localApi.resumeDocumentMonitor).toHaveBeenCalledExactlyOnceWith('session')
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledOnce()
  })

  it('hands the bound camera to a one-shot capture only after the live monitor lease is released, then resumes', async () => {
    mockUsableVideoFrame()
    const release = deferred<Awaited<ReturnType<typeof localApi.releaseDeviceLease>>>()
    vi.mocked(localApi.releaseDeviceLease).mockReturnValueOnce(release.promise)
    const view = render(<><ContinuousDocumentMonitor sessionId="session" active /><DocumentCameraCapture sessionId="session" /></>)
    await waitFor(() => expect(localApi.confirmDeviceFrame).toHaveBeenCalled())
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledOnce()
    fireEvent.click(view.getByRole('button', { name: '打开高拍仪' }))
    await waitFor(() => expect(localApi.releaseDeviceLease).toHaveBeenCalledWith('lease-document', 'document_monitor_manual_capture', expect.any(AbortSignal)))
    expect(stop).toHaveBeenCalledOnce()
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledOnce()
    expect(localApi.pauseDocumentMonitor).not.toHaveBeenCalled()
    await act(async () => { release.resolve({ lease_id: 'lease-document', status: 'released' }) })
    const close = await view.findByTitle('关闭高拍仪')
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
    fireEvent.click(close)
    await waitFor(() => expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(3))
    expect(localApi.pauseDocumentMonitor).not.toHaveBeenCalled()
  })

  it('records a normalized startup incident and keeps the active camera intent retryable', async () => {
    const incident = vi.spyOn(localApi, 'recordDeviceIncident').mockResolvedValue({} as never)
    vi.mocked(navigator.mediaDevices.getUserMedia).mockRejectedValueOnce(new DOMException('raw browser details', 'NotReadableError'))
    const view = render(<ContinuousDocumentMonitor sessionId="session" active />)
    await view.findByText('设备不可用。')
    expect(incident).toHaveBeenCalledWith('session', 'document_camera', 'DEVICE_ERROR', '设备不可用。')
    expect(localApi.pauseDocumentMonitor).not.toHaveBeenCalled()
    fireEvent.click(view.getByRole('button', { name: '重新连接高拍仪' }))
    await waitFor(() => expect(localApi.registerDevice).toHaveBeenCalledOnce())
  })

  it('privacy pause closes the document camera and allows an explicit voice resume', async () => {
    const view = render(<ContinuousDocumentMonitor sessionId="session" active />)
    await waitFor(() => expect(localApi.registerDevice).toHaveBeenCalled())
    window.dispatchEvent(new Event('hsc-privacy-pause'))
    await waitFor(() => expect(view.getByTitle('继续书写监控')).toBeTruthy())
    expect(stop).toHaveBeenCalled()
    const handled = vi.fn()
    vi.spyOn(HTMLMediaElement.prototype, 'readyState', 'get').mockReturnValue(4)
    vi.spyOn(HTMLVideoElement.prototype, 'videoWidth', 'get').mockReturnValue(1280)
    vi.spyOn(HTMLVideoElement.prototype, 'videoHeight', 'get').mockReturnValue(720)
    window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', {
      detail: { sessionId: 'session', action: 'resume', resolve: handled }
    }))
    expect(handled).not.toHaveBeenCalled()
    await waitFor(() => expect(handled).toHaveBeenCalledWith(true))
    await waitFor(() => expect(localApi.startDocumentMonitor).toHaveBeenCalledTimes(2))
  })

  it('automatically reconnects when the bound document camera returns', async () => {
    mockUsableVideoFrame()
    vi.useFakeTimers()
    const getUserMedia = vi.mocked(navigator.mediaDevices.getUserMedia)
      .mockRejectedValueOnce(new DOMException('gone', 'NotFoundError'))
      .mockResolvedValueOnce(stream)
    let view!: ReturnType<typeof render>
    // Commit the failed open and its subscription, not just the error DOM.
    await act(async () => { view = render(<ContinuousDocumentMonitor sessionId="session" active />) })
    expect(view.getAllByText(/未找到|不可用|gone/).length).toBeGreaterThan(0)
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    const reconnect = mediaListeners.get('devicechange')
    expect(reconnect).toEqual(expect.any(Function))

    // No clock advance: this must reconnect via devicechange, not backoff.
    await act(async () => { reconnect!() })

    expect(getUserMedia).toHaveBeenCalledTimes(2)
    expect(localApi.registerDevice).toHaveBeenCalledTimes(1)
    expect(view.getByText(/持续本地监控已开启/)).toBeTruthy()
  })

  it('does not claim the document camera is live before a usable first frame arrives', async () => {
    const ready = vi.spyOn(HTMLMediaElement.prototype, 'readyState', 'get').mockReturnValue(0)
    const width = vi.spyOn(HTMLVideoElement.prototype, 'videoWidth', 'get').mockReturnValue(0)
    const height = vi.spyOn(HTMLVideoElement.prototype, 'videoHeight', 'get').mockReturnValue(0)
    const view = render(<ContinuousDocumentMonitor sessionId="session" active />)
    await waitFor(() => expect(view.getByText('等待首帧')).toBeTruthy())
    expect(localApi.confirmDeviceFrame).not.toHaveBeenCalled()
    expect(view.queryByText('实时画面')).toBeNull()
    expect(view.getByText(/画面：尚未确认/)).toBeTruthy()

    ready.mockReturnValue(4)
    width.mockReturnValue(1920)
    height.mockReturnValue(1080)
    fireEvent.loadedData(view.getByLabelText('高拍仪实时画面'))
    await waitFor(() => expect(view.getByText('实时画面')).toBeTruthy())
    expect(view.getByText(/画面：1920 × 1080/)).toBeTruthy()
    expect(localApi.confirmDeviceFrame).toHaveBeenCalledExactlyOnceWith('lease-document', 1920, 1080)
    fireEvent.timeUpdate(view.getByLabelText('高拍仪实时画面'))
    expect(localApi.confirmDeviceFrame).toHaveBeenCalledTimes(1)
  })

  describe('continuous sampling and stale request boundaries', () => {
    const artifact = { artifact_id: 'page-artifact', extracted_text: '旧会话页面内容' } as Awaited<ReturnType<typeof localApi.runDocumentMonitorOcr>>
    const saved = {
      revision_id: 'page-revision', artifact_id: artifact.artifact_id, revision_index: 1,
      extracted_text: artifact.extracted_text, ocr_confidence: 0.9, review_required: true,
      intervention: { intervention_id: 'page-intervention', message: '旧会话提醒' },
    } as Awaited<ReturnType<typeof localApi.recordDocumentMonitorRevision>>
    let features: frameMonitor.DocumentFrameFeatures
    let previousPlaybackQuality: PropertyDescriptor | undefined
    const intervention = vi.fn()

    beforeEach(() => {
      vi.useFakeTimers()
      previousPlaybackQuality = Object.getOwnPropertyDescriptor(HTMLVideoElement.prototype, 'getVideoPlaybackQuality')
      // Model decoded camera frames separately from currentTime: the media
      // clock can advance even when the source is frozen.
      Object.defineProperty(HTMLVideoElement.prototype, 'getVideoPlaybackQuality', { configurable: true,
        value: () => ({ totalVideoFrames: Math.floor(performance.now() / 20), droppedVideoFrames: 0 }) })
      features = { fingerprint: new Uint8Array(48).fill(140), brightness: 180, contrast: 42, paperRatio: 0.72, skinRatio: 0.01 }
      vi.spyOn(frameMonitor, 'extractDocumentFrameFeatures').mockImplementation(() => ({ ...features,
        contentFingerprint: features.contentFingerprint ?? { width: 8, height: 8, pixels: new Uint8Array(64).fill(Math.min(255, Math.round(features.fingerprint[0] * 1.5))) } }))
      vi.spyOn(HTMLVideoElement.prototype, 'videoWidth', 'get').mockReturnValue(1280)
      vi.spyOn(HTMLVideoElement.prototype, 'videoHeight', 'get').mockReturnValue(720)
      vi.spyOn(HTMLMediaElement.prototype, 'readyState', 'get').mockReturnValue(4)
      vi.spyOn(HTMLMediaElement.prototype, 'currentTime', 'get').mockImplementation(() => Date.now() / 1000)
      vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn(), getImageData: vi.fn().mockReturnValue({}) } as unknown as CanvasRenderingContext2D)
      vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/jpeg;base64,dGVzdA==')
      vi.spyOn(localApi, 'runDocumentMonitorOcr').mockResolvedValue(artifact)
      vi.spyOn(localApi, 'recordDocumentMonitorRevision').mockResolvedValue(saved)
      vi.spyOn(localApi, 'recordDocumentMonitorEvent').mockResolvedValue({} as never)
      intervention.mockClear()
      window.addEventListener('hsc-agent-intervention', intervention)
    })
    afterEach(() => {
      window.removeEventListener('hsc-agent-intervention', intervention)
      if (previousPlaybackQuality) Object.defineProperty(HTMLVideoElement.prototype, 'getVideoPlaybackQuality', previousPlaybackQuality)
      else Reflect.deleteProperty(HTMLVideoElement.prototype, 'getVideoPlaybackQuality')
    })

    const advance = async (ms: number): Promise<void> => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }

    const ownedRevision = () => ({ ...saved, session_id: 'session', child_id: 'child', content_sha256: 'c'.repeat(64),
      page_source: { version: 'document_page_source_v1', status: 'retained', sha256: 'd'.repeat(64),
        byte_length: 128, media_type: 'image/png', encrypted: true, local_only: true,
        cloud_transmission_allowed: false, retention_reason: 'accepted_document_page_revision' } }) as typeof saved

    const warmPreview = { preview_id: 'local-preview', created_at: '2026-11-20T10:00:00Z', text: '待核对题目文字', question_count: 2, generation: 7 }
    const prepareWarmPreview = () => {
      features = { ...features, skinRatio: .184, paperRatio: .297 }
      vi.mocked(localApi.startDocumentMonitor).mockResolvedValue({
        session_id: 'session', status: 'active', generation: 7, latest_event: null, latest_revision: null
      })
      vi.spyOn(localApi, 'documentMonitorPreviews').mockResolvedValue({ items: [] })
      return vi.spyOn(localApi, 'createDocumentMonitorPreview').mockResolvedValue(warmPreview)
    }

    it('saves a single local review preview during stationary warm-color flicker without creating a formal revision', async () => {
      const preview = prepareWarmPreview()
      let samples = 0
      vi.mocked(frameMonitor.extractDocumentFrameFeatures).mockImplementation(() => ({ ...features,
        contentFingerprint: { width: 8, height: 8, pixels: new Uint8Array(64).fill(++samples % 2 ? 100 : 160) } }))
      const commit = vi.fn()
      let view!: ReturnType<typeof render>
      await act(async () => { view = render(<ContinuousDocumentMonitor sessionId="session" childId="child" active allowLocalOcrPreview onSavedRevision={commit}/>) })
      await advance(3000)
      expect(preview).toHaveBeenCalledExactlyOnceWith('session', 'dGVzdA==', 7, expect.any(String), expect.any(AbortSignal))
      expect(view.getByLabelText('本机待核对预览').textContent).toContain('待核对题目文字')
      await advance(30_000)
      expect(preview).toHaveBeenCalledTimes(1)
      expect(localApi.runDocumentMonitorOcr).not.toHaveBeenCalled()
      expect(localApi.recordDocumentMonitorRevision).not.toHaveBeenCalled()
      expect(commit).not.toHaveBeenCalled()
      expect(intervention).not.toHaveBeenCalled()
      expect(voiceCaptureReference('session', 'child').question_reference).toBeNull()
    })

    it('lists a saved preview on remount and does not send a new frame for its generation', async () => {
      const preview = prepareWarmPreview()
      vi.mocked(localApi.documentMonitorPreviews).mockResolvedValue({ items: [warmPreview] })
      let view!: ReturnType<typeof render>
      await act(async () => { view = render(<ContinuousDocumentMonitor sessionId="session" childId="child" active allowLocalOcrPreview />) })
      await advance(3000)
      expect(view.getByLabelText('本机待核对预览').textContent).toContain('待核对题目文字')
      expect(preview).not.toHaveBeenCalled()
    })

    it('does not start a local preview outside the homework preview scope', async () => {
      const preview = prepareWarmPreview()
      await act(async () => { render(<ContinuousDocumentMonitor sessionId="session" childId="child" active />) })
      await advance(4000)
      expect(localApi.documentMonitorPreviews).not.toHaveBeenCalled()
      expect(preview).not.toHaveBeenCalled()
    })

    it.each(['movement', 'occlusion', 'darkness', 'privacy', 'child switch'] as const)('ignores a late local preview after %s', async (boundary) => {
      const delayed = deferred<typeof warmPreview>()
      const preview = prepareWarmPreview().mockReturnValueOnce(delayed.promise)
      let view!: ReturnType<typeof render>
      await act(async () => { view = render(<ContinuousDocumentMonitor sessionId="session" childId="child" active allowLocalOcrPreview />) })
      await advance(3000)
      expect(preview).toHaveBeenCalledTimes(1)
      if (boundary === 'movement') features = { ...features, fingerprint: new Uint8Array(48).fill(148) }
      else if (boundary === 'occlusion') features = { ...features, skinRatio: .245 }
      else if (boundary === 'darkness') features = { ...features, brightness: 13 }
      else if (boundary === 'privacy') await act(async () => { window.dispatchEvent(new Event('hsc-privacy-pause')) })
      else await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="session" childId="other" active allowLocalOcrPreview />) })
      await advance(500)
      expect(preview.mock.calls[0][4].aborted).toBe(true)
      await act(async () => { delayed.resolve(warmPreview) })
      expect(view.queryByText('待核对题目文字')).toBeNull()
      expect(localApi.recordDocumentMonitorRevision).not.toHaveBeenCalled()
    })

    it('does not capture a preview if the exact frozen frame becomes occluded after its warm samples', async () => {
      const preview = prepareWarmPreview()
      let samples = 0
      vi.mocked(frameMonitor.extractDocumentFrameFeatures).mockImplementation(() =>
        ({ ...features, skinRatio: ++samples === 5 ? .245 : .184 }))
      await act(async () => { render(<ContinuousDocumentMonitor sessionId="session" childId="child" active allowLocalOcrPreview />) })
      await advance(2400)
      expect(preview).not.toHaveBeenCalled()
    })

    it('does not count the same decoded frame twice toward a local preview', async () => {
      const preview = prepareWarmPreview()
      Object.defineProperty(HTMLVideoElement.prototype, 'getVideoPlaybackQuality', { configurable: true,
        value: () => ({ totalVideoFrames: Math.min(30, Math.floor(performance.now() / 20)), droppedVideoFrames: 0 }) })
      await act(async () => { render(<ContinuousDocumentMonitor sessionId="session" childId="child" active allowLocalOcrPreview />) })
      await advance(4000)
      expect(preview).not.toHaveBeenCalled()
    })

    it('reads saved local history after a lost preview response without sending the image again', async () => {
      const preview = prepareWarmPreview().mockRejectedValueOnce(new Error('lost response'))
      vi.mocked(localApi.documentMonitorPreviews).mockResolvedValueOnce({ items: [] }).mockResolvedValue({ items: [warmPreview] })
      let view!: ReturnType<typeof render>
      await act(async () => { view = render(<ContinuousDocumentMonitor sessionId="session" childId="child" active allowLocalOcrPreview />) })
      await advance(25_000)
      expect(preview).toHaveBeenCalledTimes(1)
      expect(localApi.documentMonitorPreviews).toHaveBeenCalledTimes(2)
      expect(view.getByLabelText('本机待核对预览').textContent).toContain('待核对题目文字')
      expect(view.queryByText(/暂未确认保存/)).toBeNull()
    })

    it('does not OCR a periodic capture if the frozen snapshot is obstructed after the stable sample', async () => {
      features = { ...features, contentFingerprint: { width: 8, height: 8, pixels: new Uint8Array(64).fill(100) } }
      let calls = 0
      vi.mocked(frameMonitor.extractDocumentFrameFeatures).mockImplementation(() => ++calls === 5 ? { ...features, skinRatio: .8 } : features)
      await act(async () => { render(<ContinuousDocumentMonitor sessionId="session" childId="child" active />) })
      await advance(2300)
      expect(localApi.runDocumentMonitorOcr).not.toHaveBeenCalled()
    })

    it('does not save or publish an OCR response after decoded video stops advancing', async () => {
      features = { ...features, contentFingerprint: { width: 8, height: 8, pixels: new Uint8Array(64).fill(100) } }
      let frozenToken: number | null = null
      Object.defineProperty(HTMLVideoElement.prototype, 'getVideoPlaybackQuality', { configurable: true,
        value: () => ({ totalVideoFrames: frozenToken ?? Math.floor(performance.now() / 20), droppedVideoFrames: 0 }) })
      const delayed = deferred<typeof artifact>()
      vi.mocked(localApi.runDocumentMonitorOcr).mockReturnValueOnce(delayed.promise)
      vi.mocked(localApi.recordDocumentMonitorRevision).mockResolvedValue(ownedRevision())
      await act(async () => { render(<ContinuousDocumentMonitor sessionId="session" childId="child" active />) })
      await advance(2300)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledOnce()
      frozenToken = Math.floor(performance.now() / 20)
      await advance(2000)
      await act(async () => { delayed.resolve(artifact) })
      expect(localApi.recordDocumentMonitorRevision).not.toHaveBeenCalled()
      expect(voiceCaptureReference('session', 'child').question_reference).toBeNull()
    })

    it('does not publish a revision receipt if the camera freezes while saving', async () => {
      features = { ...features, contentFingerprint: { width: 8, height: 8, pixels: new Uint8Array(64).fill(100) } }
      let frozenToken: number | null = null
      Object.defineProperty(HTMLVideoElement.prototype, 'getVideoPlaybackQuality', { configurable: true,
        value: () => ({ totalVideoFrames: frozenToken ?? Math.floor(performance.now() / 20), droppedVideoFrames: 0 }) })
      const delayed = deferred<typeof saved>()
      vi.mocked(localApi.recordDocumentMonitorRevision).mockReturnValueOnce(delayed.promise)
      await act(async () => { render(<ContinuousDocumentMonitor sessionId="session" childId="child" active />) })
      await advance(2300)
      expect(localApi.recordDocumentMonitorRevision).toHaveBeenCalledOnce()
      frozenToken = Math.floor(performance.now() / 20)
      await advance(1000) // Earlier than the 1.5 s stability invalidation.
      await act(async () => { delayed.resolve(ownedRevision()) })
      expect(voiceCaptureReference('session', 'child').question_reference).toBeNull()
    })

    it('rejects newly obstructed draft before OCR even when the last sampled state was stable', async () => {
      features = { ...features, contentFingerprint: { width: 8, height: 8, pixels: new Uint8Array(64).fill(100) } }
      const capture = { current: null as ((signal: AbortSignal) => Promise<string>) | null }
      const ocr = vi.spyOn(mistakeLearningApi, 'captureReview').mockResolvedValue({ artifact_id: 'review-artifact' })
      await act(async () => { render(<ContinuousDocumentMonitor sessionId="session" childId="child" active reviewCapture={capture} />) })
      await advance(2500)
      await advance(100)
      expect(capture.current).not.toBeNull()
      features = { ...features, skinRatio: .8 }
      await expect(capture.current!(new AbortController().signal)).rejects.toThrow(/放稳|移开手/)
      expect(ocr).not.toHaveBeenCalled()
    })

    it('submits a stable, clear draft and rejects late results after camera yield', async () => {
      features = { ...features, contentFingerprint: { width: 8, height: 8, pixels: new Uint8Array(64).fill(100) } }
      const capture = { current: null as ((signal: AbortSignal) => Promise<string>) | null }
      const delayed = deferred<{ artifact_id: string }>()
      const ocr = vi.spyOn(mistakeLearningApi, 'captureReview').mockResolvedValueOnce({ artifact_id: 'review-artifact' }).mockReturnValueOnce(delayed.promise)
      let view!: ReturnType<typeof render>
      await act(async () => { view = render(<ContinuousDocumentMonitor sessionId="session" childId="child" active reviewCapture={capture} />) })
      await advance(2500)
      await advance(100)
      const draw = (HTMLCanvasElement.prototype.getContext as ReturnType<typeof vi.fn>).mock.results[0].value.drawImage as ReturnType<typeof vi.fn>
      draw.mockClear()
      const encodedDimensions: Array<[number, number]> = []
      vi.mocked(HTMLCanvasElement.prototype.toDataURL).mockImplementationOnce(function (this: HTMLCanvasElement) {
        encodedDimensions.push([this.width, this.height])
        return 'data:image/jpeg;base64,dGVzdA=='
      })
      await expect(capture.current!(new AbortController().signal)).resolves.toBe('review-artifact')
      expect(ocr).toHaveBeenCalledWith('session', 'child', 'lease-document', 'dGVzdA==', expect.any(AbortSignal))
      expect(draw).toHaveBeenCalledTimes(3)
      expect(draw.mock.calls[0][0]).toBeInstanceOf(HTMLVideoElement)
      expect(draw.mock.calls[1][0]).toBeInstanceOf(HTMLCanvasElement)
      expect(draw.mock.calls[2][0]).toBe(draw.mock.calls[1][0])
      // For a 16:9 source the encoded draft must retain both outer sides.
      expect(draw.mock.calls[2].slice(1)).toEqual([0, 0, 1280, 720])
      expect(encodedDimensions).toEqual([[1280, 720]])
      const pending = capture.current!(new AbortController().signal)
      await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="session" childId="child" active={false} reviewCapture={capture} />) })
      delayed.resolve({ artifact_id: 'late-artifact' })
      await expect(pending).rejects.toThrow(/连接|识别状态/)
    })

    it('rejects a completed OCR when the current child changes while it is pending', async () => {
      features = { ...features, contentFingerprint: { width: 8, height: 8, pixels: new Uint8Array(64).fill(100) } }
      const capture = { current: null as ((signal: AbortSignal) => Promise<string>) | null }
      const delayed = deferred<{ artifact_id: string }>()
      vi.spyOn(mistakeLearningApi, 'captureReview').mockReturnValue(delayed.promise)
      let view!: ReturnType<typeof render>
      await act(async () => { view = render(<ContinuousDocumentMonitor sessionId="session" childId="child" active reviewCapture={capture} />) })
      await advance(2500)
      await advance(100)
      const pending = capture.current!(new AbortController().signal)
      await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="session" childId="other-child" active reviewCapture={capture} />) })
      delayed.resolve({ artifact_id: 'old-child-artifact' })
      await expect(pending).rejects.toThrow(/连接|识别状态/)
    })

    it('rejects a frozen decoded frame and accepts a new one after the camera recovers', async () => {
      features = { ...features, contentFingerprint: { width: 8, height: 8, pixels: new Uint8Array(64).fill(100) } }
      let frozenToken: number | null = null
      Object.defineProperty(HTMLVideoElement.prototype, 'getVideoPlaybackQuality', { configurable: true,
        value: () => ({ totalVideoFrames: frozenToken ?? Math.floor(performance.now() / 20), droppedVideoFrames: 0 }) })
      const capture = { current: null as ((signal: AbortSignal) => Promise<string>) | null }
      const ocr = vi.spyOn(mistakeLearningApi, 'captureReview').mockResolvedValue({ artifact_id: 'review-artifact' })
      await act(async () => { render(<ContinuousDocumentMonitor sessionId="session" childId="child" active reviewCapture={capture} />) })
      await advance(2500)
      await advance(100)
      await expect(capture.current!(new AbortController().signal)).resolves.toBe('review-artifact')
      frozenToken = Math.floor(performance.now() / 20)
      await advance(1000) // Less than the existing five-second device watchdog.
      await expect(capture.current!(new AbortController().signal)).rejects.toThrow(/放稳|移开手/)
      expect(ocr).toHaveBeenCalledTimes(1)
      frozenToken = null
      await advance(100)
      await expect(capture.current!(new AbortController().signal)).resolves.toBe('review-artifact')
      expect(ocr).toHaveBeenCalledTimes(2)
    })

    it('retains the full oversized frame while scaling inside the OCR pixel budget', async () => {
      features = { ...features, contentFingerprint: { width: 8, height: 8, pixels: new Uint8Array(64).fill(100) } }
      vi.spyOn(HTMLVideoElement.prototype, 'videoWidth', 'get').mockReturnValue(5000)
      vi.spyOn(HTMLVideoElement.prototype, 'videoHeight', 'get').mockReturnValue(3000)
      const capture = { current: null as ((signal: AbortSignal) => Promise<string>) | null }
      vi.spyOn(mistakeLearningApi, 'captureReview').mockResolvedValue({ artifact_id: 'review-artifact' })
      await act(async () => { render(<ContinuousDocumentMonitor sessionId="session" childId="child" active reviewCapture={capture} />) })
      await advance(2500)
      await advance(100)
      const draw = (HTMLCanvasElement.prototype.getContext as ReturnType<typeof vi.fn>).mock.results[0].value.drawImage as ReturnType<typeof vi.fn>
      draw.mockClear()
      const encodedDimensions: Array<[number, number]> = []
      vi.mocked(HTMLCanvasElement.prototype.toDataURL).mockImplementationOnce(function (this: HTMLCanvasElement) {
        encodedDimensions.push([this.width, this.height])
        return 'data:image/jpeg;base64,dGVzdA=='
      })
      await expect(capture.current!(new AbortController().signal)).resolves.toBe('review-artifact')
      const [width, height] = encodedDimensions[0]
      expect(width * height).toBeLessThanOrEqual(4160 * 3120)
      expect(width / height).toBeCloseTo(5000 / 3000, 3)
      expect(draw.mock.calls[2].slice(1)).toEqual([0, 0, width, height])
      expect(draw.mock.calls[2][0]).toBe(draw.mock.calls[1][0])
    })

    it('submits bounded local page estimates only for advancing frames and the owned matched revision', async () => {
      vi.mocked(localApi.startDocumentMonitor).mockResolvedValue({ session_id: 'session', status: 'active', latest_event: null,
        latest_revision: null, generation: 7 } as never)
      vi.mocked(localApi.recordDocumentMonitorRevision).mockResolvedValue(ownedRevision())
      vi.spyOn(localApi, 'documentObservationEpoch').mockResolvedValue({ epoch: 'epoch', local_only: true })
      const submit = vi.spyOn(localApi, 'recordDocumentObservationSegment').mockImplementation(async (_session, data) => ({ segment_id: data.segment_id, recorded: true }))
      const previous = Object.getOwnPropertyDescriptor(HTMLVideoElement.prototype, 'getVideoPlaybackQuality')
      Object.defineProperty(HTMLVideoElement.prototype, 'getVideoPlaybackQuality', { configurable: true,
        value: () => ({ totalVideoFrames: Math.floor(performance.now() / 20) }) })
      try {
        let view!: ReturnType<typeof render>
        await act(async () => { view = render(<ContinuousDocumentMonitor sessionId="session" childId="child" active />) })
        await advance(14000)
        expect(submit).toHaveBeenCalled()
        expect(submit.mock.calls[0][1]).toMatchObject({ child_id: 'child', revision_id: 'page-revision', lease_id: 'lease-document', epoch: 'epoch', generation: 7, seq: 0 })
        expect(submit.mock.calls[0][1].visible_ms).toBeGreaterThan(0)
        expect(submit.mock.calls[0][1].visible_ms).toBeLessThanOrEqual(10000)
        view.unmount()
      } finally {
        if (previous) Object.defineProperty(HTMLVideoElement.prototype, 'getVideoPlaybackQuality', previous)
        else Reflect.deleteProperty(HTMLVideoElement.prototype, 'getVideoPlaybackQuality')
      }
    })

    it('does not attribute time to a frozen frame counter', async () => {
      vi.mocked(localApi.startDocumentMonitor).mockResolvedValue({ session_id: 'session', status: 'active', latest_event: null,
        latest_revision: null, generation: 7 } as never)
      vi.mocked(localApi.recordDocumentMonitorRevision).mockResolvedValue(ownedRevision())
      vi.spyOn(localApi, 'documentObservationEpoch').mockResolvedValue({ epoch: 'epoch', local_only: true })
      const submit = vi.spyOn(localApi, 'recordDocumentObservationSegment').mockImplementation(async (_session, data) => ({ segment_id: data.segment_id, recorded: true }))
      const previous = Object.getOwnPropertyDescriptor(HTMLVideoElement.prototype, 'getVideoPlaybackQuality')
      Object.defineProperty(HTMLVideoElement.prototype, 'getVideoPlaybackQuality', { configurable: true,
        value: () => ({ totalVideoFrames: 1 }) })
      try {
        let view!: ReturnType<typeof render>
        await act(async () => { view = render(<ContinuousDocumentMonitor sessionId="session" childId="child" active />) })
        await advance(8000)
        expect(submit).not.toHaveBeenCalled()
        view.unmount()
      } finally {
        if (previous) Object.defineProperty(HTMLVideoElement.prototype, 'getVideoPlaybackQuality', previous)
        else Reflect.deleteProperty(HTMLVideoElement.prototype, 'getVideoPlaybackQuality')
      }
    })

    it('publishes only the stable owned original and freezes onset through occlusion and analysis pause', async () => {
      vi.mocked(localApi.recordDocumentMonitorRevision).mockResolvedValue(ownedRevision())
      let view!: ReturnType<typeof render>
      await act(async () => { view = render(<ContinuousDocumentMonitor sessionId="session" childId="child" active />) })
      expect(voiceCaptureReference('session', 'child').question_reference).toBeNull()
      await advance(2500)
      const onset = voiceCaptureReference('session', 'child')
      expect(onset.question_reference).toMatchObject({ kind: 'document', binding: { revision_id: 'page-revision' } })
      features = { ...features, skinRatio: .8 }
      await advance(500)
      expect(voiceCaptureReference('session', 'child').question_reference).toBeNull()
      expect(onset.question_reference).toMatchObject({ kind: 'document', binding: { revision_id: 'page-revision' } })
      features = { ...features, skinRatio: .01 }
      await advance(2000)
      expect(voiceCaptureReference('session', 'child').question_reference?.kind).toBe('document')
      await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="session" childId="child" active analysisActive={false} />) })
      expect(voiceCaptureReference('session', 'child').question_reference).toBeNull()
      const other = { ...ownedRevision(), revision_id: 'new-owner' }
      publishVoiceDocumentCapture('session', 'child', other)
      view.unmount()
      expect(voiceCaptureReference('session', 'child').question_reference).toMatchObject({ binding: { revision_id: 'new-owner' } })
    })

    it('does not publish a late OCR after analysis is paused or a foreign source receipt', async () => {
      const delayed = deferred<typeof saved>()
      vi.mocked(localApi.recordDocumentMonitorRevision).mockReturnValue(delayed.promise)
      let view!: ReturnType<typeof render>
      await act(async () => { view = render(<ContinuousDocumentMonitor sessionId="session" childId="child" active />) })
      await advance(2500)
      expect(localApi.recordDocumentMonitorRevision).toHaveBeenCalled()
      await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="session" childId="child" active analysisActive={false} />) })
      await act(async () => { delayed.resolve(ownedRevision()) })
      expect(voiceCaptureReference('session', 'child').question_reference).toBeNull()
      vi.mocked(localApi.recordDocumentMonitorRevision).mockResolvedValue({ ...ownedRevision(), child_id: 'other' })
      await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="session" childId="child" active />) })
      await advance(12000)
      expect(voiceCaptureReference('session', 'child').question_reference).toBeNull()
    })
    async function mount(onReviewRevision = vi.fn()) {
      let view!: ReturnType<typeof render>
      await act(async () => { view = render(<ContinuousDocumentMonitor sessionId="session" active onReviewRevision={onReviewRevision} />) })
      return view
    }

    function trialPolicy() {
      vi.mocked(localApi.startDocumentMonitor).mockResolvedValue({ session_id: 'session', status: 'active', latest_event: null,
        latest_revision: null, generation: 7, trial_capture_policy: { enabled: true, interval_seconds: 30, local_only: true,
          cloud_transmission_allowed: false, diagnostic_only: true } } as never)
      return vi.spyOn(localApi, 'retainTrialDocumentFrame').mockResolvedValue({ artifact_id: 'retained-frame', image_available: true } as never)
    }

    it('retains periodic diagnostic frames during occlusion without treating them as OCR authority', async () => {
      const retained = trialPolicy()
      features = { ...features, skinRatio: 0.9 }
      const view = await mount()
      await advance(2500)
      expect(retained).toHaveBeenCalledExactlyOnceWith('session', 'dGVzdA==', 'lease-document', 7, 'periodic')
      expect(localApi.runDocumentMonitorOcr).not.toHaveBeenCalled()
      await advance(30000)
      expect(retained).toHaveBeenCalledTimes(2)
      view.unmount()
      await advance(30000)
      expect(retained).toHaveBeenCalledTimes(2)
    })

    it('binds stable-page OCR to the exact retained capture and stops saving after pause', async () => {
      const retained = trialPolicy()
      await mount()
      await advance(2500)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledWith('session', 'dGVzdA==', 'retained-frame')
      expect(retained.mock.calls.some(call => call[4] === 'ocr_capture')).toBe(true)
      const pause = requestCameraDevice('session', 'document_camera', 'pause')
      await advance(100); await pause
      const before = retained.mock.calls.length
      await advance(60000)
      expect(retained).toHaveBeenCalledTimes(before)
    })

    it('continues identification with a visible diagnostic fault when frame storage fails', async () => {
      trialPolicy().mockRejectedValue(new Error('capacity'))
      const view = await mount()
      await advance(2500)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledWith('session', 'dGVzdA==')
      expect(view.getByText(/未取得保存回执/)).toBeTruthy()
    })

    it('never retains diagnostic frames in preview-only mode', async () => {
      const retained = trialPolicy()
      await act(async () => render(<ContinuousDocumentMonitor sessionId="session" active analysisActive={false} />))
      await advance(45000)
      expect(retained).not.toHaveBeenCalled()
    })

    it('recognizes pages in preparation without retaining diagnostic frames', async () => {
      const retained = trialPolicy()
      await act(async () => render(<ContinuousDocumentMonitor sessionId="session" childId="child" active analysisActive allowDiagnosticFrameCapture={false} />))
      await advance(2500)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalled()
      await advance(45_000)
      expect(retained).not.toHaveBeenCalled()
    })

    it('recovers a saved delivery on remount without replaying its OCR, review or teaching', async () => {
      const first = await mount(); first.unmount()
      vi.mocked(localApi.pendingDocumentReceipts).mockResolvedValueOnce([{
        session_id: 'session', client_action_id: 'old-request', operation: 'revision', status: 'saved',
        local_only: true, replay_allowed: false, artifact_id: 'old-page', revision_id: 'old-version'
      }])
      const review = vi.fn()
      const restored = await mount(review)
      expect(localApi.acknowledgeDocumentReceipt).toHaveBeenCalledExactlyOnceWith('session', 'revision', 'old-request')
      expect(localApi.runDocumentMonitorOcr).not.toHaveBeenCalled()
      expect(review).not.toHaveBeenCalled()
      expect(restored.getByText(/保存结果已找回/)).toBeTruthy()
    })

    it('keeps the live document preview open before study without creating learning evidence', async () => {
      let view!: ReturnType<typeof render>
      await act(async () => {
        view = render(<ContinuousDocumentMonitor sessionId="session" active analysisActive={false} />)
      })
      await advance(15_000)
      expect(view.getByText(/当前只显示本地实时预览/)).toBeTruthy()
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      expect(localApi.startDocumentMonitor).toHaveBeenCalledOnce()
      expect(localApi.recordDocumentMonitorEvent).not.toHaveBeenCalled()
      expect(localApi.runDocumentMonitorOcr).not.toHaveBeenCalled()
    })

    it('promptly declines a page inspection before study instead of leaving it waiting', async () => {
      const view = render(<ContinuousDocumentMonitor sessionId="session" active analysisActive={false} />)
      await advance(2_000)
      const done = vi.fn()
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', {
        cancelable: true, detail: { sessionId: 'session', action: 'inspect', resolve: done }
      })))
      expect(done).toHaveBeenCalledExactlyOnceWith(false)
      expect(localApi.runDocumentMonitorOcr).not.toHaveBeenCalled()
      expect(view.queryByRole('button', { name: '检查当前页' })).toBeNull()
    })

    it('ends an outstanding inspection when the learning stage ends', async () => {
      const view = await mount()
      const done = vi.fn()
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', {
        cancelable: true, detail: { sessionId: 'session', action: 'inspect', resolve: done }
      })))
      expect(done).not.toHaveBeenCalled()
      await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="session" active analysisActive={false} />) })
      expect(done).toHaveBeenCalledExactlyOnceWith(false)
      await advance(180_000)
      expect(done).toHaveBeenCalledTimes(1)
    })

    it('does not auto-capture after restart while the original delivery remains unconfirmed', async () => {
      vi.mocked(localApi.pendingDocumentReceipts).mockResolvedValueOnce([{
        session_id: 'session', client_action_id: 'old-request', operation: 'ocr', status: 'unconfirmed', local_only: true, replay_allowed: false
      }])
      const restored = await mount()
      await advance(60_000)
      expect(localApi.runDocumentMonitorOcr).not.toHaveBeenCalled()
      expect(localApi.acknowledgeDocumentReceipt).not.toHaveBeenCalled()
      expect(restored.getByText(/上次页面操作还有未确认/)).toBeTruthy()
      const done = vi.fn()
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', { detail: { sessionId: 'session', action: 'inspect', resolve: done } })))
      await advance(2_000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledOnce()
      expect(done).toHaveBeenCalledExactlyOnceWith(true)
    })

    it('keeps small writing made during an OCR request eligible for a new saved page', async () => {
      features.contentFingerprint = { width: 80, height: 100, pixels: new Uint8Array(8000).fill(128) }
      const pending = deferred<typeof artifact>()
      vi.mocked(localApi.runDocumentMonitorOcr).mockReturnValueOnce(pending.promise)
      await mount()
      await advance(2500)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(1)
      const newContent = { ...features.contentFingerprint, pixels: features.contentFingerprint.pixels.slice() }
      newContent.pixels.set([40, 40, 40, 40], 2200)
      features = { ...features, contentFingerprint: newContent }
      await advance(2500)
      await act(async () => { pending.resolve(artifact) })
      // The older snapshot was superseded, so it must not become a revision.
      expect(localApi.recordDocumentMonitorRevision).not.toHaveBeenCalled()
      await advance(1500)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(1)
      await advance(1000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(2)
      expect(localApi.recordDocumentMonitorRevision).toHaveBeenCalledTimes(1)
      await advance(16000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(2)
      expect(JSON.stringify(vi.mocked(localApi.recordDocumentMonitorEvent).mock.calls)).not.toContain('contentFingerprint')
      expect(vi.mocked(localApi.recordDocumentMonitorRevision).mock.calls.every(call => call[2].length === 96)).toBe(true)
    })

    it('restores a saved pause after remount and only resumes with a real frame receipt', async () => {
      vi.mocked(localApi.documentMonitorStatus).mockResolvedValue({ session_id: 'session', status: 'paused', latest_event: null, latest_revision: null })
      const view = await mount()
      expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled()
      expect(queryCameraDevices('session', 'document_camera').devices[0].frame_confirmed).toBe(false)
      const resumed = requestCameraDevice('session', 'document_camera', 'resume')
      await advance(100)
      expect(await resumed).toEqual({ device_type: 'document_camera', device_status: 'active', frame_confirmed: true, lease_confirmed: true, persisted_status: 'active' })
      const pause = requestCameraDevice('session', 'document_camera', 'pause')
      await advance(100)
      expect(await pause).toMatchObject({ device_status: 'paused', frame_confirmed: false })
      expect(stop).toHaveBeenCalled()
      view.unmount(); vi.mocked(navigator.mediaDevices.getUserMedia).mockClear()
      await mount()
      expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled()
    })

    it('keeps document resume failed when persistence or frames are unavailable', async () => {
      vi.mocked(localApi.documentMonitorStatus).mockResolvedValue({ session_id: 'session', status: 'paused', latest_event: null, latest_revision: null })
      await mount()
      vi.mocked(localApi.resumeDocumentMonitor).mockRejectedValueOnce(new Error('not saved'))
      const failedSave = requestCameraDevice('session', 'document_camera', 'resume').catch((error) => error)
      await advance(100)
      expect(await failedSave).toBeInstanceOf(Error)
      expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled()
      vi.mocked(HTMLMediaElement.prototype, true)
      vi.spyOn(HTMLMediaElement.prototype, 'readyState', 'get').mockReturnValue(0)
      const noFrame = requestCameraDevice('session', 'document_camera', 'resume').catch((error) => error)
      await advance(5_200)
      expect(await noFrame).toBeInstanceOf(Error)
      expect(stop).toHaveBeenCalled()
    })

    it('treats ordinary hand occlusion as a silent transient frame without loosening capture gates', async () => {
      features.skinRatio = 0.9
      const view = await mount()
      await advance(2_000)
      expect(intervention).not.toHaveBeenCalled()
      expect(localApi.runDocumentMonitorOcr).not.toHaveBeenCalled()
      features.skinRatio = 0.01; await advance(1_000)
      features.skinRatio = 0.9; await advance(21_000)
      expect(intervention.mock.calls.some(([event]) => event.detail.sourceType === 'document_monitor_framing' && event.detail.message.includes('遮'))).toBe(false)
      expect(localApi.runDocumentMonitorOcr).not.toHaveBeenCalled()
      view.unmount(); intervention.mockClear()
      await mount(); await advance(2_000)
      expect(intervention).not.toHaveBeenCalled()
    })

    it.each(['pause', 'privacy', 'session', 'unmount'].flatMap((boundary) => ['ocr', 'revision'].map((phase) => ({ boundary, phase }))))('ignores a late $phase response after $boundary', async ({ boundary, phase }) => {
      const ocr = deferred<typeof artifact>()
      const revision = deferred<typeof saved>()
      if (phase === 'ocr') vi.mocked(localApi.runDocumentMonitorOcr).mockReturnValueOnce(ocr.promise)
      else vi.mocked(localApi.recordDocumentMonitorRevision).mockReturnValueOnce(revision.promise)
      const review = vi.fn()
      const view = await mount(review)
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', { detail: { sessionId: 'session', action: 'inspect' } })))
      await advance(2_000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(1)
      await act(async () => {
        if (boundary === 'privacy') window.dispatchEvent(new Event('hsc-privacy-pause'))
        else if (boundary === 'unmount') view.unmount()
        else view.rerender(<ContinuousDocumentMonitor sessionId={boundary === 'session' ? 'next-session' : 'session'} active={boundary === 'session'} onReviewRevision={review} />)
      })
      expect(stop).toHaveBeenCalled()
      await act(async () => {
        if (phase === 'ocr') ocr.resolve(artifact)
        else revision.resolve(saved)
      })
      expect(localApi.recordDocumentMonitorRevision).toHaveBeenCalledTimes(phase === 'ocr' ? 0 : 1)
      expect(review).not.toHaveBeenCalled()
      expect(intervention).not.toHaveBeenCalled()
      expect(view.queryByText(artifact.extracted_text)).toBeNull()
      if (boundary === 'session') expect(localApi.startDocumentMonitor).toHaveBeenLastCalledWith('next-session')
    })

    it('does not report a stale OCR error as a failure in the new session', async () => {
      const ocr = deferred<typeof artifact>()
      vi.mocked(localApi.runDocumentMonitorOcr).mockReturnValueOnce(ocr.promise)
      const view = await mount()
      await advance(2_000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(1)
      await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="next-session" active />) })
      await act(async () => { ocr.reject(new Error('old request failed')) })
      expect(vi.mocked(localApi.recordDocumentMonitorEvent).mock.calls.some(([, state]) => state === 'capture_failed')).toBe(false)
      expect(view.queryByText(/识别失败/)).toBeNull()
    })

    it('keeps one OCR in flight and captures the latest settled page instead of queuing intermediate pages', async () => {
      const ocr = deferred<typeof artifact>()
      vi.mocked(localApi.runDocumentMonitorOcr).mockReturnValueOnce(ocr.promise)
      await mount()
      await advance(2_000)
      features = { ...features, fingerprint: new Uint8Array(48).fill(70) }
      await advance(20_000)
      features = { ...features, fingerprint: new Uint8Array(48).fill(90) }
      await advance(2_000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(1)
      await act(async () => { ocr.resolve(artifact) })
      // The first attempt is already older than the 15-second throttle and
      // was discarded; the latest stable page may be sent on the next tick.
      await advance(500)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(2)
      expect(localApi.recordDocumentMonitorRevision).toHaveBeenLastCalledWith('session', artifact.artifact_id, frameMonitor.fingerprintSignature(features.fingerprint))
    })

    it('discards an OCR interrupted by the hand and retries only a new stable frame sooner', async () => {
      const ocr = deferred<typeof artifact>()
      const old = { ...artifact, artifact_id: 'old-stale-artifact' }
      const current = { ...artifact, artifact_id: 'fresh-artifact' }
      const currentSaved = { ...saved, artifact_id: current.artifact_id, revision_id: 'fresh-revision' }
      vi.mocked(localApi.runDocumentMonitorOcr).mockReturnValueOnce(ocr.promise).mockResolvedValue(current)
      vi.mocked(localApi.recordDocumentMonitorRevision).mockResolvedValue(currentSaved)
      await mount()
      await advance(2000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(1)
      features = { ...features, skinRatio: .8 }
      await advance(500)
      await act(async () => { ocr.resolve(old) })
      expect(localApi.recordDocumentMonitorRevision).not.toHaveBeenCalled()
      features = { ...features, skinRatio: .01, fingerprint: new Uint8Array(48).fill(110) }
      await advance(4500)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(2)
      expect(localApi.recordDocumentMonitorRevision).toHaveBeenCalledExactlyOnceWith('session', current.artifact_id, frameMonitor.fingerprintSignature(features.fingerprint))
    })

    it('records bounded metadata for an interrupted OCR and fresh retry without page contents', async () => {
      const previous = Object.getOwnPropertyDescriptor(window, 'desktop')
      const record = vi.fn().mockResolvedValue(undefined)
      Object.defineProperty(window, 'desktop', { configurable: true, value: { recordRendererDiagnostic: record } })
      try {
        const ocr = deferred<typeof artifact>()
        vi.mocked(localApi.runDocumentMonitorOcr).mockReturnValueOnce(ocr.promise).mockResolvedValue(artifact)
        await mount(); await advance(2000)
        features = { ...features, skinRatio: .8 }; await advance(500)
        await act(async () => { ocr.resolve(artifact) })
        features = { ...features, skinRatio: .01, fingerprint: new Uint8Array(48).fill(110) }
        await advance(4500)
        const events = record.mock.calls.map(([entry]) => entry.event)
        expect(events.filter((event) => event === 'document.monitor.ocr_started')).toHaveLength(2)
        expect(events).toContain('document.monitor.ocr_discarded')
        expect(events).toContain('document.monitor.ocr_retry_considered')
        expect(events).toContain('document.monitor.revision_saved')
        expect(JSON.stringify(record.mock.calls)).not.toContain(artifact.extracted_text)
        expect(JSON.stringify(record.mock.calls)).not.toContain('dGVzdA')
      } finally {
        if (previous) Object.defineProperty(window, 'desktop', previous)
        else Reflect.deleteProperty(window, 'desktop')
      }
    })

    it('rate limits continuous frame-state diagnostic events during rapid changes', async () => {
      const previous = Object.getOwnPropertyDescriptor(window, 'desktop')
      const record = vi.fn().mockResolvedValue(undefined)
      Object.defineProperty(window, 'desktop', { configurable: true, value: { recordRendererDiagnostic: record } })
      try {
        await mount()
        for (let i = 0; i < 40; i++) {
          features = { ...features, skinRatio: i % 2 ? .8 : .01 }
          await advance(500)
        }
        const states = record.mock.calls.filter(([entry]) => entry.event === 'document.monitor.frame_state')
        expect(states.length).toBeGreaterThan(0)
        expect(states.length).toBeLessThanOrEqual(5)
      } finally {
        if (previous) Object.defineProperty(window, 'desktop', previous)
        else Reflect.deleteProperty(window, 'desktop')
      }
    })

    it('records one bounded pending alert for a long OCR and stops it after pause', async () => {
      const previous = Object.getOwnPropertyDescriptor(window, 'desktop')
      const record = vi.fn().mockResolvedValue(undefined)
      Object.defineProperty(window, 'desktop', { configurable: true, value: { recordRendererDiagnostic: record } })
      try {
        const ocr = deferred<typeof artifact>()
        vi.mocked(localApi.runDocumentMonitorOcr).mockReturnValueOnce(ocr.promise)
        const view = await mount(); await advance(2000)
        features = { ...features, skinRatio: .8 }; await advance(31_000)
        expect(record.mock.calls.filter(([entry]) => entry.event === 'document.monitor.ocr_pending')).toHaveLength(1)
        await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="session" active={false} />) })
        await act(async () => { ocr.resolve(artifact) })
        await advance(40_000)
        expect(record.mock.calls.filter(([entry]) => entry.event === 'document.monitor.ocr_pending')).toHaveLength(1)
      } finally {
        if (previous) Object.defineProperty(window, 'desktop', previous)
        else Reflect.deleteProperty(window, 'desktop')
      }
    })

    it.each(['ocr', 'revision'] as const)('classifies a %s failure without mislabeling the other stage', async (phase) => {
      const previous = Object.getOwnPropertyDescriptor(window, 'desktop')
      const record = vi.fn().mockResolvedValue(undefined)
      Object.defineProperty(window, 'desktop', { configurable: true, value: { recordRendererDiagnostic: record } })
      try {
        if (phase === 'ocr') vi.mocked(localApi.runDocumentMonitorOcr).mockRejectedValueOnce(new Error('PRIVATE_ERROR'))
        else vi.mocked(localApi.recordDocumentMonitorRevision).mockRejectedValueOnce(new Error('PRIVATE_ERROR'))
        await mount(); await advance(2000)
        const events = record.mock.calls.map(([entry]) => entry.event)
        expect(events).toContain(`document.monitor.${phase}_failed`)
        expect(events).not.toContain(`document.monitor.${phase === 'ocr' ? 'revision' : 'ocr'}_failed`)
        expect(JSON.stringify(record.mock.calls)).not.toContain('PRIVATE_ERROR')
      } finally {
        if (previous) Object.defineProperty(window, 'desktop', previous)
        else Reflect.deleteProperty(window, 'desktop')
      }
    })

    it.each(['ocr', 'revision'] as const)('does not label an unconfirmed %s result as failed', async (phase) => {
      const previous = Object.getOwnPropertyDescriptor(window, 'desktop')
      const record = vi.fn().mockResolvedValue(undefined)
      Object.defineProperty(window, 'desktop', { configurable: true, value: { recordRendererDiagnostic: record } })
      try {
        const unknown = new ApiError('unconfirmed', undefined, 'DOCUMENT_OPERATION_RESULT_UNCONFIRMED')
        if (phase === 'ocr') vi.mocked(localApi.runDocumentMonitorOcr).mockRejectedValueOnce(unknown)
        else vi.mocked(localApi.recordDocumentMonitorRevision).mockRejectedValueOnce(unknown)
        await mount(); await advance(2000)
        const events = record.mock.calls.map(([entry]) => entry.event)
        expect(events).toContain(`document.monitor.${phase}_unconfirmed`)
        expect(events).not.toContain(`document.monitor.${phase}_failed`)
      } finally {
        if (previous) Object.defineProperty(window, 'desktop', previous)
        else Reflect.deleteProperty(window, 'desktop')
      }
    })

    it('does not let a synchronous diagnostics failure interrupt OCR', async () => {
      const previous = Object.getOwnPropertyDescriptor(window, 'desktop')
      Object.defineProperty(window, 'desktop', { configurable: true, value: { recordRendererDiagnostic: () => { throw new Error('diagnostics offline') } } })
      try {
        await mount(); await advance(2000)
        expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledOnce()
        expect(localApi.recordDocumentMonitorRevision).toHaveBeenCalledOnce()
      } finally {
        if (previous) Object.defineProperty(window, 'desktop', previous)
        else Reflect.deleteProperty(window, 'desktop')
      }
    })

    it('does not accelerate when only diagnostic frame retention was interrupted before OCR', async () => {
      const pending = deferred<{ artifact_id: string; image_available: boolean }>()
      trialPolicy().mockImplementation(async (_session, _image, _lease, _generation, reason) =>
        reason === 'ocr_capture' ? pending.promise : { artifact_id: 'diagnostic-frame', image_available: true } as never)
      await mount(); await advance(2000)
      expect(localApi.runDocumentMonitorOcr).not.toHaveBeenCalled()
      features = { ...features, skinRatio: .8 }; await advance(500)
      await act(async () => { pending.resolve({ artifact_id: 'retained-frame', image_available: true }) })
      features = { ...features, skinRatio: .01 }; await advance(5000)
      expect(localApi.runDocumentMonitorOcr).not.toHaveBeenCalled()
      await advance(11_000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledOnce()
    })

    it('does not accelerate another OCR if a revision was already being saved when the hand appeared', async () => {
      const revision = deferred<typeof saved>()
      vi.mocked(localApi.recordDocumentMonitorRevision).mockReturnValueOnce(revision.promise)
      await mount(); await advance(2000)
      expect(localApi.recordDocumentMonitorRevision).toHaveBeenCalledOnce()
      features = { ...features, skinRatio: .8 }; await advance(500)
      await act(async () => { revision.resolve(saved) })
      features = { ...features, skinRatio: .01, fingerprint: new Uint8Array(48).fill(110) }
      await advance(5000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(1)
      expect(localApi.recordDocumentMonitorRevision).toHaveBeenCalledTimes(1)
      await advance(11_000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(2)
    })

    it('records a saved revision as historical when the page changes while saving', async () => {
      const previous = Object.getOwnPropertyDescriptor(window, 'desktop')
      const record = vi.fn().mockResolvedValue(undefined)
      Object.defineProperty(window, 'desktop', { configurable: true, value: { recordRendererDiagnostic: record } })
      try {
        const revision = deferred<typeof saved>()
        vi.mocked(localApi.recordDocumentMonitorRevision).mockReturnValueOnce(revision.promise)
        await mount(); await advance(2000)
        features = { ...features, skinRatio: .8 }; await advance(500)
        await act(async () => { revision.resolve(saved) })
        const events = record.mock.calls.map(([entry]) => entry.event)
        expect(events).toContain('document.monitor.revision_saved')
        expect(events).toContain('document.monitor.revision_historical')
        expect(events).not.toContain('document.monitor.ocr_discarded')
        expect(events).not.toContain('document.monitor.ocr_retry_considered')
      } finally {
        if (previous) Object.defineProperty(window, 'desktop', previous)
        else Reflect.deleteProperty(window, 'desktop')
      }
    })

    it('does not accelerate an OCR after analysis was suspended and resumed', async () => {
      const ocr = deferred<typeof artifact>()
      vi.mocked(localApi.runDocumentMonitorOcr).mockReturnValueOnce(ocr.promise)
      const view = await mount(); await advance(2000)
      features = { ...features, skinRatio: .8 }; await advance(500)
      await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="session" active analysisActive={false} />) })
      await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="session" active analysisActive />) })
      await act(async () => { ocr.resolve(artifact) })
      features = { ...features, skinRatio: .01 }; await advance(5000)
      expect(localApi.recordDocumentMonitorRevision).not.toHaveBeenCalled()
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(1)
    })

    it('does not schedule another capture from a rejected duplicate inspection', async () => {
      const ocr = deferred<typeof artifact>()
      vi.mocked(localApi.runDocumentMonitorOcr).mockReturnValueOnce(ocr.promise)
      const review = vi.fn(), first = vi.fn(), duplicate = vi.fn()
      await mount(review)
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', { detail: { sessionId: 'session', action: 'inspect', resolve: first } })))
      await advance(2_000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledOnce()
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', { detail: { sessionId: 'session', action: 'inspect', resolve: duplicate } })))
      expect(duplicate).toHaveBeenCalledExactlyOnceWith(false)
      await act(async () => { ocr.resolve(artifact) })
      expect(first).toHaveBeenCalledExactlyOnceWith(true)
      expect(review).toHaveBeenCalledExactlyOnceWith(saved)
      await advance(17_000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledOnce()
      expect(review).toHaveBeenCalledOnce()
    })

    it.each(['inspect', 'review'])('leaves fresh %s confirmation to its original goal without a second review or reminder', async action => {
      const review = vi.fn(), done = vi.fn(), report = vi.fn()
      await mount(review)
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', {
        detail: { sessionId: 'session', action, deferReview: true, resolve: done, report }
      })))
      await advance(2_000)
      expect(done).toHaveBeenCalledExactlyOnceWith(true)
      expect(report).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ artifact_id: artifact.artifact_id, revision_id: saved.revision_id }))
      expect(review).not.toHaveBeenCalled()
      expect(intervention).not.toHaveBeenCalled()
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', { detail: { sessionId: 'session', action: 'review' } })))
      expect(review).toHaveBeenCalledExactlyOnceWith(saved)
    })

    it('hands a marked current page to the mistake dialogue without a competing OCR or behavior prompt', async () => {
      const marked = { ...saved, marked_question_count: 1 }
      vi.mocked(localApi.recordDocumentMonitorRevision).mockResolvedValueOnce(marked)
      const review = vi.fn(() => 'mistake' as const), done = vi.fn(), report = vi.fn()
      await mount(review)
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', {
        detail: { sessionId: 'session', action: 'inspect', resolve: done, report }
      })))
      await advance(2_000)
      expect(review).toHaveBeenCalledExactlyOnceWith(marked)
      expect(done).toHaveBeenCalledExactlyOnceWith(true)
      expect(report).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ artifact_id: marked.artifact_id }))
      expect(intervention).not.toHaveBeenCalled()
    })

    it('hands an unchanged page to the original goal without a second local review or OCR', async () => {
      const review = vi.fn(), done = vi.fn(), report = vi.fn()
      await mount(review); await advance(2_000)
      intervention.mockClear()
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', {
        detail: { sessionId: 'session', action: 'review', deferReview: true, resolve: done, report }
      })))
      expect(done).toHaveBeenCalledExactlyOnceWith(true)
      expect(report).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ revision_id: saved.revision_id }))
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledOnce()
      expect(review).not.toHaveBeenCalled()
      expect(intervention).not.toHaveBeenCalled()
    })

    it.each([undefined, 'another-session'])('ignores a page review without its own session (%s)', async sessionId => {
      const review = vi.fn(), done = vi.fn(), report = vi.fn()
      await mount(review); await advance(2_000)
      const event = new CustomEvent('hsc-document-monitor-voice-action', {
        cancelable: true, detail: { sessionId, action: 'review', resolve: done, report }
      })
      act(() => window.dispatchEvent(event))
      expect(event.defaultPrevented).toBe(false)
      expect(done).not.toHaveBeenCalled()
      expect(report).not.toHaveBeenCalled()
      expect(review).not.toHaveBeenCalled()
    })

    it('ignores a page command already owned by an earlier receiver', async () => {
      const review = vi.fn(), done = vi.fn(), report = vi.fn()
      await mount(review); await advance(2_000)
      const event = new CustomEvent('hsc-document-monitor-voice-action', {
        cancelable: true, detail: { sessionId: 'session', action: 'review', resolve: done, report }
      })
      event.preventDefault()
      act(() => window.dispatchEvent(event))
      expect(done).not.toHaveBeenCalled()
      expect(report).not.toHaveBeenCalled()
      expect(review).not.toHaveBeenCalled()
    })

    it('does not let an inactive foreign receiver settle the current inspection', async () => {
      await act(async () => { render(<ContinuousDocumentMonitor sessionId="old-session" active={false} />) })
      const review = vi.fn()
      await mount(review); await advance(2_000)
      let pending!: Promise<boolean>
      act(() => { pending = requestOwnedVoiceEvent('hsc-document-monitor-voice-action', {
        sessionId: 'session', action: 'review'
      }) })
      expect(await pending).toBe(true)
      expect(review).toHaveBeenCalledExactlyOnceWith(saved)
    })

    it('lets only one live same-session receiver return the original page receipt', async () => {
      const first = vi.fn(), second = vi.fn(), report = vi.fn()
      await mount(first)
      await mount(second)
      await advance(2_000)
      let pending!: Promise<boolean>
      act(() => { pending = requestOwnedVoiceEvent('hsc-document-monitor-voice-action', {
        sessionId: 'session', action: 'review', report
      }) })
      expect(await pending).toBe(true)
      expect(first).toHaveBeenCalledExactlyOnceWith(saved)
      expect(second).not.toHaveBeenCalled()
      expect(report).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ revision_id: saved.revision_id }))
    })

    it('owns a slow page check beyond the legacy deadline and resolves only its saved page', async () => {
      const review = vi.fn(), done = vi.fn(), report = vi.fn()
      await mount(review); await advance(2_000)
      const ocr = deferred<typeof artifact>()
      vi.mocked(localApi.runDocumentMonitorOcr).mockReturnValueOnce(ocr.promise)
      let pending!: Promise<boolean>
      act(() => { pending = requestOwnedVoiceEvent('hsc-document-monitor-voice-action', { sessionId: 'session', action: 'inspect', report }).then(value => { done(value); return value }) })
      await advance(50_000)
      expect(done).not.toHaveBeenCalled()
      expect(review).not.toHaveBeenCalled()
      await act(async () => { ocr.resolve(artifact); await Promise.resolve() })
      expect(await pending).toBe(true)
      expect(review).toHaveBeenCalledExactlyOnceWith(saved)
      expect(report).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ revision_id: saved.revision_id }))
    })

    it.each(['ocr', 'revision'])('does not speak or review a page changed while %s was pending', async stage => {
      const review = vi.fn(), handled = vi.fn(), report = vi.fn(), intervention = vi.fn()
      await mount(review); await advance(2_000)
      const late = deferred<any>()
      if (stage === 'ocr') vi.mocked(localApi.runDocumentMonitorOcr).mockReturnValueOnce(late.promise)
      else vi.mocked(localApi.recordDocumentMonitorRevision).mockReturnValueOnce(late.promise)
      features = { ...features, fingerprint: new Uint8Array(48).fill(70) }
      window.addEventListener('hsc-agent-intervention', intervention)
      try {
        act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', {
          detail: { sessionId: 'session', action: 'review', resolve: handled, report }
        })))
        await advance(17_000)
        expect(handled).not.toHaveBeenCalled()
        features = { ...features, fingerprint: new Uint8Array(48).fill(90) }
        intervention.mockClear()
        await act(async () => { late.resolve(stage === 'ocr' ? artifact : saved); await Promise.resolve() })
        expect(review).not.toHaveBeenCalled()
        expect(handled).not.toHaveBeenCalled()
        expect(report).not.toHaveBeenCalled()
        expect(intervention).not.toHaveBeenCalled()
        const next = { ...artifact, artifact_id: 'new-paper', extracted_text: 'New page' }
        const nextSaved = { ...saved, artifact_id: next.artifact_id, revision_id: 'new-paper-version', extracted_text: next.extracted_text }
        vi.mocked(localApi.runDocumentMonitorOcr).mockResolvedValue(next)
        vi.mocked(localApi.recordDocumentMonitorRevision).mockResolvedValue(nextSaved)
        await advance(17_000)
        expect(review).toHaveBeenCalledExactlyOnceWith(nextSaved)
        expect(handled).toHaveBeenCalledExactlyOnceWith(true)
        expect(report).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ artifact_id: next.artifact_id }))
      } finally { window.removeEventListener('hsc-agent-intervention', intervention) }
    })

    it('ends a hung inspection without claiming success, replaying it, or starting concurrent OCR', async () => {
      const review = vi.fn(), done = vi.fn(), report = vi.fn()
      await mount(review); await advance(2_000)
      const ocr = deferred<typeof artifact>()
      vi.mocked(localApi.runDocumentMonitorOcr).mockReturnValueOnce(ocr.promise)
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', {
        detail: { sessionId: 'session', action: 'inspect', resolve: done, report }
      })))
      await advance(181_000)
      expect(done).toHaveBeenCalledExactlyOnceWith(false)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(2)
      await act(async () => { ocr.resolve(artifact); await Promise.resolve() })
      expect(done).toHaveBeenCalledTimes(1)
      expect(report).not.toHaveBeenCalled()
      expect(review).not.toHaveBeenCalled()
    })

    it('does not automatically resubmit after an unknown OCR result, but accepts a new voice check', async () => {
      const view = await mount()
      vi.mocked(localApi.runDocumentMonitorOcr).mockRejectedValueOnce(new ApiError('Result unknown', undefined, 'DOCUMENT_OPERATION_RESULT_UNCONFIRMED'))
      await advance(60_000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledOnce()
      expect(view.getByText(/没有确认保存结果/)).toBeTruthy()
      const done = vi.fn()
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', {
        detail: { sessionId: 'session', action: 'inspect', resolve: done }
      })))
      await advance(2_000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(2)
      expect(done).toHaveBeenCalledExactlyOnceWith(true)
    })

    it('reviews an unchanged live page with its saved source receipt without another OCR', async () => {
      const review = vi.fn(), handled = vi.fn(), report = vi.fn()
      await mount(review); await advance(2_000)
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', {
        detail: { sessionId: 'session', action: 'review', resolve: handled, report }
      })))
      expect(review).toHaveBeenCalledExactlyOnceWith(saved)
      expect(handled).toHaveBeenCalledExactlyOnceWith(true)
      expect(report).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ artifact_id: artifact.artifact_id, revision_id: saved.revision_id }))
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledOnce()
    })

    it.each(['page', 'ink'])('waits for the current %s instead of reviewing the previous OCR after a change between timer ticks', async change => {
      features.contentFingerprint = { width: 80, height: 100, pixels: new Uint8Array(8000).fill(128) }
      const review = vi.fn(), handled = vi.fn(), report = vi.fn()
      await mount(review); await advance(2_000)
      const freshArtifact = { ...artifact, artifact_id: 'review-current-page', extracted_text: '当前页的题目' }
      const freshSaved = { ...saved, artifact_id: freshArtifact.artifact_id, revision_id: 'review-current-version', extracted_text: freshArtifact.extracted_text }
      if (change === 'page') features = { ...features, fingerprint: new Uint8Array(48).fill(70), contentFingerprint: undefined }
      else {
        const pixels = features.contentFingerprint!.pixels.slice()
        pixels.set([40, 40, 40, 40], 2200)
        features = { ...features, contentFingerprint: { ...features.contentFingerprint!, pixels } }
      }
      vi.mocked(localApi.runDocumentMonitorOcr).mockResolvedValue(freshArtifact)
      vi.mocked(localApi.recordDocumentMonitorRevision).mockResolvedValue(freshSaved)
      // No timer advance: a cached previous-frame decision is insufficient.
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', {
        detail: { sessionId: 'session', action: 'review', resolve: handled, report }
      })))
      expect(review).not.toHaveBeenCalled()
      expect(handled).not.toHaveBeenCalled()
      await advance(17_000)
      expect(review).toHaveBeenCalledExactlyOnceWith(freshSaved)
      expect(handled).toHaveBeenCalledExactlyOnceWith(true)
      expect(report).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ artifact_id: freshArtifact.artifact_id, revision_id: freshSaved.revision_id }))
    })

    it('does not review an old page through occlusion and cancels the pending review without replay', async () => {
      const review = vi.fn(), handled = vi.fn(), controller = new AbortController()
      await mount(review); await advance(2_000)
      features = { ...features, skinRatio: .9 }
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', {
        detail: { sessionId: 'session', action: 'review', resolve: handled, signal: controller.signal }
      })))
      expect(review).not.toHaveBeenCalled()
      await advance(17_000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledOnce()
      expect(handled).not.toHaveBeenCalled()
      act(() => controller.abort())
      expect(handled).toHaveBeenCalledExactlyOnceWith(false)
      features = { ...features, skinRatio: .01 }; await advance(20_000)
      expect(review).not.toHaveBeenCalled()
    })

    it('does not capture or ask for review for an already cancelled inspection', async () => {
      const review = vi.fn(), handled = vi.fn(), controller = new AbortController()
      await mount(review); await advance(2_000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledOnce()
      controller.abort()
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', {
        detail: { sessionId: 'session', action: 'inspect', signal: controller.signal, resolve: handled }
      })))
      expect(handled).toHaveBeenCalledExactlyOnceWith(false)
      await advance(17_000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledOnce()
      expect(review).not.toHaveBeenCalled()
    })

    it.each(['ocr', 'revision'] as const)('does not let a yielded %s response consume the next inspection receipt', async phase => {
      const ocr = deferred<typeof artifact>(), revision = deferred<typeof saved>()
      const freshArtifact = { ...artifact, artifact_id: 'fresh-page', extracted_text: '新页面题目' }
      const freshSaved = { ...saved, artifact_id: 'fresh-page', revision_id: 'fresh-revision', extracted_text: '新页面题目' }
      if (phase === 'ocr') vi.mocked(localApi.runDocumentMonitorOcr).mockReturnValueOnce(ocr.promise)
      else vi.mocked(localApi.recordDocumentMonitorRevision).mockReturnValueOnce(revision.promise)
      const review = vi.fn(), first = vi.fn(), next = vi.fn(), report = vi.fn()
      const view = await mount(review)
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', { detail: { sessionId: 'session', action: 'inspect', resolve: first } })))
      await advance(2_000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledOnce()
      await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="session" active={false} onReviewRevision={review} />) })
      expect(first).toHaveBeenCalledExactlyOnceWith(false)
      await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="session" active onReviewRevision={review} />) })
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', { detail: { sessionId: 'session', action: 'inspect', resolve: next, report } })))
      await advance(2_000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledOnce()
      vi.mocked(localApi.runDocumentMonitorOcr).mockResolvedValue(freshArtifact)
      vi.mocked(localApi.recordDocumentMonitorRevision).mockResolvedValue(freshSaved)
      await act(async () => { if (phase === 'ocr') ocr.resolve(artifact); else revision.resolve(saved) })
      expect(first).toHaveBeenCalledOnce()
      expect(next).not.toHaveBeenCalled()
      expect(report).not.toHaveBeenCalled()
      await advance(2_000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(1)
      await advance(13_000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(2)
      expect(next).toHaveBeenCalledExactlyOnceWith(true)
      expect(report).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ artifact_id: 'fresh-page', revision_id: 'fresh-revision' }))
      expect(review).toHaveBeenCalledExactlyOnceWith(freshSaved)
    })

    it('retries failed OCR after the minimum interval without waiting five minutes for an unchanged page', async () => {
      vi.mocked(localApi.runDocumentMonitorOcr).mockRejectedValueOnce(new Error('temporary failure'))
      await mount()
      await advance(2_000)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(1)
      await advance(14_500)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(1)
      await advance(500)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(2)
      expect(localApi.recordDocumentMonitorRevision).toHaveBeenCalledTimes(1)
    })

    it.each([undefined, 'DOCUMENT_OCR_BUSY'])('retries backend busy without a failure event or blocking voice (code: %s)', async (code) => {
      vi.mocked(localApi.runDocumentMonitorOcr).mockRejectedValueOnce(new ApiError('DOCUMENT_OCR_BUSY', 409, code))
      const view = await mount()
      await advance(2_000)
      expect(view.getByText(/页面解析正忙/)).toBeTruthy()
      expect(localApi.recordDocumentMonitorRevision).not.toHaveBeenCalled()
      expect(vi.mocked(localApi.recordDocumentMonitorEvent).mock.calls.some(([, state]) => state === 'capture_failed')).toBe(false)
      const handled = vi.fn()
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', { detail: { sessionId: 'session', action: 'inspect', resolve: handled } })))
      expect(handled).not.toHaveBeenCalled()
      await advance(14_500)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(1)
      await advance(500)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(2)
      expect(handled).toHaveBeenCalledExactlyOnceWith(true)
    })

    it.each(['status', 'state'])('reports an unconfigured cloud fallback as blocked using %s even when local OCR returned some text', async (field) => {
      vi.mocked(localApi.runDocumentMonitorOcr).mockResolvedValue({
        ...artifact, details: { lines: [], engine: 'local', cloud_vision_fallback: { [field]: 'blocked', source: 'not_configured' } },
      } as typeof artifact)
      const view = await mount()
      await advance(2_000)
      expect(view.getByText(/页面云端解析暂不可用/)).toBeTruthy()
      expect(view.getByText(/原视频不上传；合格页内图按配置解析/)).toBeTruthy()
      expect(view.queryByText(/云端图片调用 0|原始画面不保存、不上传/)).toBeNull()
      expect(view.getByText(artifact.extracted_text)).toBeTruthy()
    })

    it('displays cloud page readings as unconfirmed without changing the review callback or local text', async () => {
      vi.mocked(localApi.runDocumentMonitorOcr).mockResolvedValue({
        ...artifact, details: { lines: [], engine: 'local', cloud_vision_fallback: { status: 'needs_review', blocks: [{ text: '未核对的云端猜测' }], privacy: { uploaded: 'page_region_binary_ink' } } },
      } as typeof artifact)
      const review = vi.fn()
      const view = await mount(review)
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', { detail: { sessionId: 'session', action: 'inspect' } })))
      await advance(2_000)
      expect(view.getByText(/云端已给出待核对文字/)).toBeTruthy()
      expect(view.getByText(/本次上传了页内二值文字图/)).toBeTruthy()
      expect(view.getByText(artifact.extracted_text)).toBeTruthy()
      expect(view.getByText('未核对的云端猜测')).toBeTruthy()
      expect(review).toHaveBeenCalledExactlyOnceWith(saved)
      expect(review.mock.calls[0][0].extracted_text).toBe(artifact.extracted_text)
      expect(localApi.recordDocumentMonitorRevision).toHaveBeenCalledExactlyOnceWith('session', artifact.artifact_id, expect.any(String))
    })

    it('rotates the live preview from the OCR orientation result without altering the raw capture stream', async () => {
      vi.mocked(localApi.runDocumentMonitorOcr).mockResolvedValue({
        ...artifact,
        details: {
          lines: [], engine: 'local',
          local_pipeline: {
            engine: 'paddleocr', route: 'printed_text',
            quality: {
              passed: true, issues: [], metrics: {},
              transforms: { orientation_degrees: 90, orientation_auto_corrected: true },
              orientation_evidence: { ambiguous: false, review_reasons: [] },
            },
            question_blocks: [], manual_review: { required: false, reasons: [], handwriting: 'none', math: 'none' },
            queue: { task_id: 'orientation-check', behavior_fps_cap: 2 },
          },
        },
      } as never)
      const view = await mount()
      await advance(2_000)
      const video = view.getByLabelText('高拍仪实时画面') as HTMLVideoElement
      expect(video.dataset.autoOrientation).toBe('90')
      expect(video.style.transform).toContain('rotate(90deg)')
      expect(view.getByText('已自动转正 90°')).toBeTruthy()
      expect(video.srcObject).toBe(stream)
    })

    it('does not rotate the live preview when orientation evidence is absent', async () => {
      vi.mocked(localApi.runDocumentMonitorOcr).mockResolvedValue({
        ...artifact,
        details: {
          lines: [], engine: 'local',
          local_pipeline: {
            engine: 'paddleocr', route: 'printed_text',
            quality: { passed: true, issues: [], metrics: {}, transforms: { orientation_degrees: 270, orientation_auto_corrected: true } },
            question_blocks: [], manual_review: { required: false, reasons: [], handwriting: 'none', math: 'none' },
            queue: { task_id: 'legacy-orientation', behavior_fps_cap: 2 },
          },
        },
      } as never)
      const view = await mount()
      await advance(2_000)
      const video = view.getByLabelText('高拍仪实时画面') as HTMLVideoElement
      expect(video.dataset.autoOrientation).toBe('0')
      expect(video.style.transform).toContain('rotate(0deg)')
      expect(view.queryByText(/已自动转正/)).toBeNull()
    })

    it('shows cloud-only text without claiming local OCR found it or submitting it as a confirmed question', async () => {
      vi.mocked(localApi.runDocumentMonitorOcr).mockResolvedValue({
        ...artifact, extracted_text: '', details: { lines: [], engine: 'local', cloud_vision_fallback: { status: 'needs_review', blocks: [{ text: '云端识别的待核对公式' }] } },
      } as typeof artifact)
      vi.mocked(localApi.recordDocumentMonitorRevision).mockResolvedValue({ ...saved, extracted_text: '', intervention: undefined })
      const review = vi.fn()
      const view = await mount(review)
      const handled = vi.fn()
      const report = vi.fn()
      act(() => window.dispatchEvent(new CustomEvent('hsc-document-monitor-voice-action', { detail: { sessionId: 'session', action: 'inspect', resolve: handled, report } })))
      await advance(2_000)
      expect(view.getByText('云端识别的待核对公式')).toBeTruthy()
      expect(view.getByText(/云端已给出待核对文字/)).toBeTruthy()
      expect(view.queryByText(/没有识别到可靠文字/)).toBeNull()
      expect(review).not.toHaveBeenCalled()
      expect(intervention).not.toHaveBeenCalled()
      expect(handled).toHaveBeenCalledExactlyOnceWith(true)
      expect(report).toHaveBeenCalledWith(expect.objectContaining({
        page_status: 'text_available',
        response_text: expect.stringContaining('云端页内文字候选是：“云端识别的待核对公式”'),
      }))
    })

    it.each([
      ['DOCUMENT_VISION_NOT_CONFIGURED', '尚未配置云端页面解析'],
      ['DOCUMENT_VISION_BUDGET_EXCEEDED', '预算不足'],
      ['DOCUMENT_VISION_FACE_DETECTED', '画面包含人脸'],
      ['DOCUMENT_VISION_PII_DETECTED', '页面可能含个人信息'],
      ['DOCUMENT_VISION_PAGE_BOUNDARY_UNPROVEN', '无法确认安全的页面边界'],
      ['DOCUMENT_VISION_PRIVACY_CHECK_UNAVAILABLE', '本地安全检查暂不可用'],
      ['DOCUMENT_VISION_CLOUD_NOT_ALLOWED', '未获授权'],
      ['DOCUMENT_VISION_RATE_LIMITED', '正在限流'],
      ['DOCUMENT_VISION_TIMEOUT', '响应超时'],
    ])('explains the cloud boundary %s without claiming success', async (code, message) => {
      vi.mocked(localApi.runDocumentMonitorOcr).mockResolvedValue({
        ...artifact, details: { lines: [], engine: 'local', cloud_vision_fallback: { status: 'blocked', error_code: code } },
      } as typeof artifact)
      const view = await mount()
      await advance(2_000)
      expect(view.getByText(new RegExp(message))).toBeTruthy()
      expect(view.queryByText(/云端已给出待核对文字|本次上传了页内二值文字图/)).toBeNull()
      expect(view.queryByText(code)).toBeNull()
    })

    it('discards cloud text returned after changing sessions', async () => {
      const ocr = deferred<typeof artifact>()
      vi.mocked(localApi.runDocumentMonitorOcr).mockReturnValueOnce(ocr.promise)
      const view = await mount()
      await advance(2_000)
      await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="next-session" active />) })
      await act(async () => { ocr.resolve({
        ...artifact, details: { lines: [], engine: 'local', cloud_vision_fallback: { status: 'needs_review', blocks: [{ text: '旧会话的云端文字' }] } },
      } as typeof artifact) })
      expect(view.queryByLabelText('云端页面解析')).toBeNull()
      expect(view.queryByText('旧会话的云端文字')).toBeNull()
      expect(localApi.recordDocumentMonitorRevision).not.toHaveBeenCalled()
    })

    it('does not restart the sampling clock when the parent supplies a new review callback', async () => {
      const view = await mount()
      for (let i = 0; i < 20; i += 1) {
        await advance(100)
        view.rerender(<ContinuousDocumentMonitor sessionId="session" active onReviewRevision={vi.fn()} />)
      }
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(1)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    })

    it('releases and reconnects a stalled document stream when the system clock moves backwards', async () => {
      await act(async () => { render(<ContinuousDocumentMonitor sessionId="session" active analysisActive={false} />) })
      const video = document.querySelector('video')!
      Object.defineProperty(video, 'getVideoPlaybackQuality', { configurable: true, value: () => ({ totalVideoFrames: 12, droppedVideoFrames: 0 }) })
      await advance(1_000)
      expect(queryCameraDevices('session', 'document_camera').devices[0].frame_confirmed).toBe(true)
      vi.setSystemTime(Date.now() - 3_600_000)
      await advance(5_000)
      expect(localApi.releaseDeviceLease).toHaveBeenCalledExactlyOnceWith('lease-document', 'document_monitor_frame_timeout', expect.any(AbortSignal))
      expect(stop).toHaveBeenCalledTimes(1)
      await advance(2_000)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
      expect(localApi.runDocumentMonitorOcr).not.toHaveBeenCalled()
    })

    it.each([true, false])('retires decoded-stalled frames while media time advances (analysis $0)', async (analysisActive) => {
      await act(async () => { render(<ContinuousDocumentMonitor sessionId="session" active analysisActive={analysisActive} />) })
      const video = document.querySelector('video')!
      let frames = 12
      Object.defineProperty(video, 'getVideoPlaybackQuality', { configurable: true, value: () => ({ totalVideoFrames: frames, droppedVideoFrames: 0 }) })
      // The original currentTime fixture keeps advancing; only decoded frames stop.
      await advance(1_000)
      expect(queryCameraDevices('session', 'document_camera').devices[0].frame_confirmed).toBe(true)
      await advance(5_000)
      expect(localApi.releaseDeviceLease).toHaveBeenCalledExactlyOnceWith('lease-document', 'document_monitor_frame_timeout', expect.any(AbortSignal))
      expect(stop).toHaveBeenCalledTimes(1)
      const ocrCount = vi.mocked(localApi.runDocumentMonitorOcr).mock.calls.length
      await advance(1_999)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      expect(localApi.runDocumentMonitorOcr).toHaveBeenCalledTimes(ocrCount)
      frames += 1
      await advance(1)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
      expect(localApi.releaseDeviceLease).toHaveBeenCalledTimes(1)
      if (!analysisActive) expect(localApi.runDocumentMonitorOcr).not.toHaveBeenCalled()
    })

    it('keeps advancing decoded frames healthy when media time is fixed', async () => {
      vi.mocked(Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'currentTime')!.get!).mockReturnValue(1)
      await mount()
      const video = document.querySelector('video')!
      Object.defineProperty(video, 'getVideoPlaybackQuality', { configurable: true, value: () => ({ totalVideoFrames: Date.now(), droppedVideoFrames: 0 }) })
      await advance(10_000)
      expect(localApi.releaseDeviceLease).not.toHaveBeenCalled()
      expect(stop).not.toHaveBeenCalled()
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      expect(queryCameraDevices('session', 'document_camera').devices[0].frame_confirmed).toBe(true)
    })

    it('reconnects at the backoff boundary after the track ends', async () => {
      await mount()
      act(() => trackListeners.get('ended')?.())
      expect(stop).toHaveBeenCalledTimes(1)
      await advance(1_999)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      await advance(1)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
    })

    it.each(['muted','enabled'] as const)('retires live %s even with advancing frames and waits for original release ACK', async flag => {
      const released = deferred<Awaited<ReturnType<typeof localApi.releaseDeviceLease>>>()
      vi.mocked(localApi.releaseDeviceLease).mockReturnValueOnce(released.promise)
      await mount()
      const video = document.querySelector('video')!
      Object.defineProperty(video, 'getVideoPlaybackQuality', { configurable: true, value: () => ({ totalVideoFrames: performance.now(), droppedVideoFrames: 0 }) })
      expect(queryCameraDevices('session','document_camera').devices[0].frame_confirmed).toBe(true)
      track[flag] = flag === 'muted'
      await advance(6100)
      expect(stop).toHaveBeenCalledTimes(1)
      expect(localApi.releaseDeviceLease).toHaveBeenCalledExactlyOnceWith('lease-document','document_monitor_frame_timeout',expect.any(AbortSignal))
      await advance(2500)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      track.enabled = true; track.muted = false
      await act(async()=>{released.resolve({lease_id:'lease-document',status:'released'})})
      await advance(100)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
    })
    it('does not restart a document stream that recovers from a short mute', async () => {
      await mount()
      track.muted = true
      await advance(2000)
      expect(queryCameraDevices('session','document_camera').devices[0].frame_confirmed).toBe(false)
      track.muted = false
      await advance(4000)
      expect(queryCameraDevices('session','document_camera').devices[0].frame_confirmed).toBe(true)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      expect(stop).not.toHaveBeenCalled()
    })

    it('waits for the old lease release acknowledgment before reconnecting a stalled feed', async () => {
      const released = deferred<Awaited<ReturnType<typeof localApi.releaseDeviceLease>>>()
      vi.mocked(localApi.releaseDeviceLease).mockReturnValueOnce(released.promise)
      Object.defineProperty(HTMLVideoElement.prototype, 'getVideoPlaybackQuality', { configurable: true,
        value: () => ({ totalVideoFrames: 1, droppedVideoFrames: 0 }) })
      const view = await mount()
      await advance(5_500)
      expect(localApi.releaseDeviceLease).toHaveBeenCalledExactlyOnceWith('lease-document', 'document_monitor_frame_timeout', expect.any(AbortSignal))
      expect(stop).toHaveBeenCalledTimes(1)
      await advance(2_000)
      // The backend still owns the old lease even though browser tracks stopped.
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      expect(localApi.startDocumentMonitor).toHaveBeenCalledTimes(1)
      expect(localApi.registerDevice).toHaveBeenCalledTimes(1)
      expect(view.queryByText(/持续本地监控已开启/)).toBeNull()
      await act(async () => { released.resolve({ lease_id: 'lease-document', status: 'released' }) })
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
      expect(localApi.startDocumentMonitor).toHaveBeenCalledTimes(2)
      expect(view.getByText(/持续本地监控已开启/)).toBeTruthy()
    })

    it('keeps failed lease releases blocked and retries them before opening hardware again', async () => {
      vi.mocked(localApi.releaseDeviceLease)
        .mockRejectedValueOnce(new Error('backend unavailable'))
        .mockResolvedValueOnce({ lease_id: 'lease-document', status: 'active' } as never)
        .mockResolvedValueOnce({ lease_id: 'lease-document', status: 'released' })
      const view = await mount()
      await act(async () => { trackListeners.get('ended')?.() })
      await advance(2_000)
      expect(localApi.releaseDeviceLease).toHaveBeenCalledTimes(2)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      expect(localApi.startDocumentMonitor).toHaveBeenCalledTimes(1)
      expect(view.getByText(/高拍仪还在关闭/)).toBeTruthy()
      await advance(3_999)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      await advance(1)
      expect(localApi.releaseDeviceLease).toHaveBeenCalledTimes(3)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
      expect(view.getByText(/持续本地监控已开启/)).toBeTruthy()
    })

    it('does not reopen after privacy pause while release acknowledgment is pending', async () => {
      const released = deferred<Awaited<ReturnType<typeof localApi.releaseDeviceLease>>>()
      vi.mocked(localApi.releaseDeviceLease).mockReturnValueOnce(released.promise)
      const view = await mount()
      await act(async () => { trackListeners.get('ended')?.() })
      await advance(2_000)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      await act(async () => { window.dispatchEvent(new Event('hsc-privacy-pause')) })
      await act(async () => { released.resolve({ lease_id: 'lease-document', status: 'released' }) })
      await advance(30_000)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      expect(localApi.registerDevice).toHaveBeenCalledTimes(1)
      expect(view.getByTitle('继续书写监控')).toBeTruthy()
    })

    it('does not accept a released receipt belonging to another lease', async () => {
      vi.mocked(localApi.releaseDeviceLease).mockResolvedValueOnce({ lease_id: 'foreign-lease', status: 'released' })
        .mockResolvedValueOnce({ lease_id: 'foreign-lease', status: 'released' })
      const view = await mount()
      await act(async () => { trackListeners.get('ended')?.() })
      await advance(2_000)
      expect(localApi.releaseDeviceLease).toHaveBeenCalledTimes(2)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      expect(view.getByText(/高拍仪还在关闭/)).toBeTruthy()
    })

    it('opens only the new session after an old release wait is superseded', async () => {
      const released = deferred<Awaited<ReturnType<typeof localApi.releaseDeviceLease>>>()
      vi.mocked(localApi.releaseDeviceLease).mockReturnValueOnce(released.promise)
      const view = await mount()
      await act(async () => { trackListeners.get('ended')?.() })
      await advance(2_000)
      await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="next-session" active />) })
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      await act(async () => { released.resolve({ lease_id: 'lease-document', status: 'released' }) })
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
      expect(localApi.startDocumentMonitor).toHaveBeenCalledTimes(2)
      expect(localApi.startDocumentMonitor).toHaveBeenLastCalledWith('next-session')
      expect(localApi.registerDevice).toHaveBeenLastCalledWith('next-session', 'document_camera', expect.any(String), expect.any(Object))
    })

    it('retains a failed late registration release until acknowledged before opening the new session', async () => {
      const registered = deferred<Awaited<ReturnType<typeof localApi.registerDevice>>>()
      const released = deferred<Awaited<ReturnType<typeof localApi.releaseDeviceLease>>>()
      vi.mocked(localApi.registerDevice).mockReturnValueOnce(registered.promise)
      vi.mocked(localApi.releaseDeviceLease).mockReturnValueOnce(released.promise)
        .mockResolvedValueOnce({ lease_id: 'late-lease', status: 'released' })
      const view = await mount()
      await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="next-session" active />) })
      await act(async () => { registered.resolve({ lease_id: 'late-lease', status: 'active', expires_at: new Date().toISOString() }) })
      expect(localApi.releaseDeviceLease).toHaveBeenCalledExactlyOnceWith('late-lease', 'monitor_superseded', expect.any(AbortSignal))
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      expect(stop).toHaveBeenCalledTimes(1)
      await act(async () => { released.reject(new Error('temporary release failure')) })
      expect(localApi.releaseDeviceLease).toHaveBeenNthCalledWith(2, 'late-lease', 'document_monitor_reconnect', expect.any(AbortSignal))
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
      expect(localApi.registerDevice).toHaveBeenLastCalledWith('next-session', 'document_camera', expect.any(String), expect.any(Object))
    })

    it('coalesces duplicate device-return notifications while waiting for release', async () => {
      const released = deferred<Awaited<ReturnType<typeof localApi.releaseDeviceLease>>>()
      vi.mocked(localApi.releaseDeviceLease).mockReturnValueOnce(released.promise)
      await mount()
      await act(async () => { trackListeners.get('ended')?.() })
      const retry = mediaListeners.get('devicechange')!
      expect(retry).toEqual(expect.any(Function))
      await act(async () => { retry(); retry() })
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      await act(async () => { released.resolve({ lease_id: 'lease-document', status: 'released' }) })
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
      expect(localApi.releaseDeviceLease).toHaveBeenCalledTimes(1)
      expect(localApi.registerDevice).toHaveBeenCalledTimes(2)
    })

    it('retires a never-settling release and retries before reopening after bounded failure', async () => {
      vi.mocked(localApi.releaseDeviceLease).mockReturnValueOnce(new Promise(() => undefined))
        .mockReturnValueOnce(new Promise(() => undefined))
      const view = await mount()
      await act(async () => { trackListeners.get('ended')?.() })
      await advance(4_999)
      expect(localApi.releaseDeviceLease).toHaveBeenCalledTimes(1)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      await advance(1)
      expect(vi.mocked(localApi.releaseDeviceLease).mock.calls[0][2]?.aborted).toBe(true)
      expect(localApi.releaseDeviceLease).toHaveBeenCalledTimes(2)
      await advance(5_000)
      expect(vi.mocked(localApi.releaseDeviceLease).mock.calls[1][2]?.aborted).toBe(true)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      expect(view.getByText(/高拍仪还在关闭/)).toBeTruthy()
      await advance(3_999)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      await advance(1)
      expect(localApi.releaseDeviceLease).toHaveBeenCalledTimes(3)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
      expect(view.getByText(/持续本地监控已开启/)).toBeTruthy()
    })

    it('does not accept a late timed-out release response in place of the current retry acknowledgment', async () => {
      const original = deferred<Awaited<ReturnType<typeof localApi.releaseDeviceLease>>>()
      const retry = deferred<Awaited<ReturnType<typeof localApi.releaseDeviceLease>>>()
      vi.mocked(localApi.releaseDeviceLease).mockReturnValueOnce(original.promise).mockReturnValueOnce(retry.promise)
      await mount()
      await act(async () => { trackListeners.get('ended')?.() })
      await advance(5_000)
      expect(localApi.releaseDeviceLease).toHaveBeenCalledTimes(2)
      await act(async () => { original.resolve({ lease_id: 'lease-document', status: 'released' }) })
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      await act(async () => { retry.resolve({ lease_id: 'lease-document', status: 'released' }) })
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
    })

    it('retries a hung old lease only for the current session after timeout', async () => {
      vi.mocked(localApi.releaseDeviceLease).mockReturnValueOnce(new Promise(() => undefined))
      const view = await mount()
      await act(async () => { trackListeners.get('ended')?.() })
      await advance(2_000)
      await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="next-session" active />) })
      await advance(3_000)
      expect(localApi.releaseDeviceLease).toHaveBeenCalledTimes(2)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
      expect(localApi.startDocumentMonitor).toHaveBeenLastCalledWith('next-session')
      expect(localApi.registerDevice).toHaveBeenLastCalledWith('next-session', 'document_camera', expect.any(String), expect.any(Object))
    })

    it.each(['privacy', 'inactive'])('does not reopen after %s pause while the release transport remains hung', async (pause) => {
      vi.mocked(localApi.releaseDeviceLease).mockReturnValueOnce(new Promise(() => undefined))
      const view = await mount()
      await act(async () => { trackListeners.get('ended')?.() })
      await advance(2_000)
      await act(async () => {
        if (pause === 'privacy') window.dispatchEvent(new Event('hsc-privacy-pause'))
        else view.rerender(<ContinuousDocumentMonitor sessionId="session" active={false} />)
      })
      await advance(30_000)
      expect(vi.mocked(localApi.releaseDeviceLease).mock.calls[0][2]?.aborted).toBe(true)
      expect(localApi.releaseDeviceLease).toHaveBeenCalledTimes(1)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      expect(localApi.registerDevice).toHaveBeenCalledTimes(1)
      if (pause === 'inactive') {
        await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="session" active />) })
        expect(localApi.releaseDeviceLease).toHaveBeenCalledTimes(2)
        expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
      }
    })

    it.each(['missing', 'frozen'])('reconnects a %s video feed without needing a child action', async (feed) => {
      if (feed === 'missing') vi.mocked(Object.getOwnPropertyDescriptor(HTMLVideoElement.prototype, 'videoWidth')!.get!).mockReturnValue(0)
      else Object.defineProperty(HTMLVideoElement.prototype, 'getVideoPlaybackQuality', { configurable: true,
        value: () => ({ totalVideoFrames: 1, droppedVideoFrames: 0 }) })
      await mount()
      await advance(feed === 'missing' ? 12_000 : 5_500)
      expect(localApi.releaseDeviceLease).toHaveBeenCalledWith('lease-document', feed === 'missing' ? 'document_camera_first_frame_timeout' : 'document_monitor_frame_timeout', expect.any(AbortSignal))
      await advance(2_000)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
    })

    it('does not let a pending play promise prevent no-frame recovery or affect the next connection', async () => {
      const play = deferred<void>()
      vi.mocked(HTMLMediaElement.prototype.play).mockReturnValueOnce(play.promise)
      vi.mocked(Object.getOwnPropertyDescriptor(HTMLVideoElement.prototype, 'videoWidth')!.get!).mockReturnValue(0)
      const view = await mount()
      await advance(12_000)
      expect(localApi.releaseDeviceLease).toHaveBeenCalledWith('lease-document', 'document_camera_first_frame_timeout', expect.any(AbortSignal))
      vi.mocked(Object.getOwnPropertyDescriptor(HTMLVideoElement.prototype, 'videoWidth')!.get!).mockReturnValue(1280)
      await advance(2_000)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
      await act(async () => { play.reject(new Error('old playback failure')) })
      expect(stop).toHaveBeenCalledTimes(1)
      expect(view.getByText(/持续本地监控已开启/)).toBeTruthy()
    })

    it('keeps reconnect timers cancelled while paused and reopens automatically when activity resumes', async () => {
      const view = await mount()
      act(() => trackListeners.get('ended')?.())
      view.rerender(<ContinuousDocumentMonitor sessionId="session" active={false} />)
      await advance(30_000)
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="session" active />) })
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
    })

    it('ignores an old lease renewal failure after switching sessions', async () => {
      const renewal = deferred<Awaited<ReturnType<typeof localApi.renewDeviceLease>>>()
      vi.mocked(localApi.renewDeviceLease).mockReturnValueOnce(renewal.promise)
      const view = await mount()
      await advance(60_000)
      expect(localApi.renewDeviceLease).toHaveBeenCalledTimes(1)
      await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="next-session" active />) })
      const stopped = stop.mock.calls.length
      await act(async () => { renewal.reject(new Error('old lease')) })
      expect(stop).toHaveBeenCalledTimes(stopped)
      expect(view.getByText(/持续本地监控已开启/)).toBeTruthy()
    })

    it('does not open hardware for a superseded start request and opens the new session once', async () => {
      const start = deferred<Awaited<ReturnType<typeof localApi.startDocumentMonitor>>>()
      vi.mocked(localApi.startDocumentMonitor).mockReturnValueOnce(start.promise)
      const view = await mount()
      await act(async () => { view.rerender(<ContinuousDocumentMonitor sessionId="next-session" active />) })
      expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled()
      await act(async () => { start.resolve({ session_id: 'session', status: 'active', latest_event: null, latest_revision: null }) })
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
      expect(localApi.registerDevice).toHaveBeenCalledExactlyOnceWith('next-session', 'document_camera', expect.any(String), expect.any(Object))
    })

    it('stops a late getUserMedia result without registering it after privacy pause', async () => {
      const media = deferred<MediaStream>()
      vi.mocked(navigator.mediaDevices.getUserMedia).mockReturnValueOnce(media.promise)
      await mount()
      act(() => window.dispatchEvent(new Event('hsc-privacy-pause')))
      await act(async () => { media.resolve(stream) })
      expect(stop).toHaveBeenCalledTimes(1)
      expect(localApi.registerDevice).not.toHaveBeenCalled()
    })

    it('releases an acquired stream during pending autofocus and never registers it later', async () => {
      const focus = deferred<Awaited<ReturnType<typeof configureDocumentCameraAutofocus>>>()
      vi.mocked(configureDocumentCameraAutofocus).mockReturnValueOnce(focus.promise)
      const view = await mount()
      view.rerender(<ContinuousDocumentMonitor sessionId="session" active={false} />)
      expect(stop).toHaveBeenCalledTimes(1)
      await act(async () => { focus.resolve({ exposed: false, applied: false, requested_mode: null, active_mode: null }) })
      expect(localApi.registerDevice).not.toHaveBeenCalled()
    })
  })
})
