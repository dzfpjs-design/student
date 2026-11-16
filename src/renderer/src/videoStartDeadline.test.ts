import { afterEach, describe, expect, it, vi } from 'vitest'
import { openDocumentCameraStream, openObservationCameraStream } from './mediaDevices'

afterEach(() => vi.useRealTimers())

describe('bound video acquisition deadline', () => {
  it('quarantines the saved binding when Chromium rotates the pending device id', async () => {
    vi.useFakeTimers()
    let complete!: (stream: MediaStream) => void
    const pending = new Promise<MediaStream>(resolve => { complete = resolve })
    const acquire = vi.fn(() => pending)
    const failure = openObservationCameraStream('previous-browser-id', 'L523', acquire, 'saved-binding').catch(error => error)
    await vi.advanceTimersByTimeAsync(20_000)
    expect((await failure).name).toBe('CameraStartTimeoutError')
    const stop = vi.fn()
    const stream = { getTracks: () => [{ stop }] } as unknown as MediaStream
    try {
      const reopening = openObservationCameraStream('rotated-browser-id', 'L523', acquire, 'saved-binding').catch(error => error)
      await vi.advanceTimersByTimeAsync(0)
      expect((await Promise.race([reopening, Promise.resolve({ name: 'still_pending' })])).name).toBe('CameraStartPendingError')
      await expect(openDocumentCameraStream('rotated-browser-id', 'monitor', acquire, 'saved-binding')).rejects.toHaveProperty('name', 'CameraStartPendingError')
      expect(acquire).toHaveBeenCalledTimes(1)
    } finally {
      complete(stream)
      await vi.advanceTimersByTimeAsync(0)
    }
    expect(stop).toHaveBeenCalledTimes(1)
    await expect(openObservationCameraStream('rotated-browser-id', 'L523', async () => stream, 'saved-binding')).resolves.toBe(stream)
  })

  it('ends a hung scanner start, quarantines it across roles, and stops a late stream', async () => {
    vi.useFakeTimers()
    let complete!: (stream: MediaStream) => void
    const acquire = vi.fn(() => new Promise<MediaStream>(resolve => { complete = resolve }))
    const stop = vi.fn()
    const stream = { getTracks: () => [{ stop }] } as unknown as MediaStream
    const failure = openDocumentCameraStream('deadline-late-scanner', 'monitor', acquire).catch(error => error)
    await vi.advanceTimersByTimeAsync(20_000)
    expect((await Promise.race([failure, Promise.resolve({ name: 'still_pending' })])).name).toBe('CameraStartTimeoutError')
    await expect(openDocumentCameraStream('deadline-late-scanner', 'capture', acquire)).rejects.toHaveProperty('name', 'CameraStartPendingError')
    await expect(openObservationCameraStream('deadline-late-scanner', '', acquire)).rejects.toHaveProperty('name', 'CameraStartPendingError')
    expect(acquire).toHaveBeenCalledTimes(1)
    complete(stream)
    await vi.advanceTimersByTimeAsync(0)
    expect(stop).toHaveBeenCalledTimes(1)
    await expect(openDocumentCameraStream('deadline-late-scanner', 'capture', async () => stream)).resolves.toBe(stream)
    expect(stop).toHaveBeenCalledTimes(1)
  })

  it('does not advance the resolution ladder after a timed-out native request later rejects', async () => {
    vi.useFakeTimers()
    let fail!: (cause: unknown) => void
    const acquire = vi.fn(() => new Promise<MediaStream>((_, reject) => { fail = reject }))
    const failure = openDocumentCameraStream('deadline-late-rejection', 'capture', acquire).catch(error => error)
    await vi.advanceTimersByTimeAsync(20_000)
    expect((await Promise.race([failure, Promise.resolve({ name: 'still_pending' })])).name).toBe('CameraStartTimeoutError')
    fail(new DOMException('Timeout starting video source', 'AbortError'))
    await vi.advanceTimersByTimeAsync(5_000)
    expect(acquire).toHaveBeenCalledTimes(1)
    const stream = { getTracks: () => [] } as unknown as MediaStream
    await expect(openDocumentCameraStream('deadline-late-rejection', 'monitor', async () => stream)).resolves.toBe(stream)
  })

  it('holds the device between transient retries but leaves another physical device available', async () => {
    vi.useFakeTimers()
    const stream = { getTracks: () => [] } as unknown as MediaStream
    const acquire = vi.fn().mockRejectedValueOnce(new DOMException('busy', 'NotReadableError')).mockResolvedValueOnce(stream)
    const started = openDocumentCameraStream('deadline-retry-scanner', 'capture', acquire)
    await vi.advanceTimersByTimeAsync(100)
    await expect(openDocumentCameraStream('deadline-retry-scanner', 'monitor', acquire)).rejects.toHaveProperty('name', 'CameraStartPendingError')
    await expect(openObservationCameraStream('deadline-independent-camera', '', async () => stream)).resolves.toBe(stream)
    await vi.advanceTimersByTimeAsync(1_100)
    await expect(started).resolves.toBe(stream)
    expect(acquire).toHaveBeenCalledTimes(2)
    expect(acquire.mock.calls.every(([constraints]) => constraints.video.width.exact === 4160)).toBe(true)
  })

  it('bounds the entire scanner ladder and never starts another native request after expiry', async () => {
    vi.useFakeTimers()
    const acquire = vi.fn(() => new Promise<MediaStream>((_, reject) => {
      setTimeout(() => reject(new DOMException('busy', 'NotReadableError')), 1_500)
    }))
    const failure = openDocumentCameraStream('deadline-whole-ladder', 'capture', acquire).catch(error => error)
    await vi.advanceTimersByTimeAsync(20_000)
    expect((await Promise.race([failure, Promise.resolve({ name: 'still_pending' })])).name).toBe('CameraStartTimeoutError')
    const callsAtDeadline = acquire.mock.calls.length
    await vi.advanceTimersByTimeAsync(10_000)
    expect(acquire).toHaveBeenCalledTimes(callsAtDeadline)
  })
})
