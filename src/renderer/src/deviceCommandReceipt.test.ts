import { afterEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { CAPTURE_SAVED_EVENT, createCaptureReceiptBridge, emitCaptureReceipt, guardCommandApi, isImmediatePrivacyStop, SupersededDeviceCommand } from './deviceCommandReceipt'
import type { AgentTurn } from './types'

const turn = { agent_turn_id: 'turn-camera', action: { type: 'request_capture' } } as AgentTurn
const cleanups: Array<() => void> = []
afterEach(() => {
  cleanups.splice(0).forEach((cleanup) => cleanup())
  localStorage.clear()
  vi.useRealTimers()
  vi.restoreAllMocks()
})
function bridge(sessionId = 'session-a') {
  const onError = vi.fn()
  const receipt = createCaptureReceiptBridge(sessionId, onError)
  cleanups.push(receipt.start())
  return { ...receipt, onError }
}

describe('capture command receipts', () => {
  it('announces a persisted receipt only after the server acknowledges it', async () => {
    let resolve!: (turn: AgentTurn) => void
    vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(() => new Promise(done => { resolve = done }))
    const saved = vi.fn()
    window.addEventListener(CAPTURE_SAVED_EVENT, saved)
    cleanups.push(() => window.removeEventListener(CAPTURE_SAVED_EVENT, saved))
    const receipts = bridge()
    receipts.request(turn, true, request => request.accept())
    emitCaptureReceipt({ sessionId: 'session-a', commandId: turn.agent_turn_id, artifactId: 'recaptured-page', status: 'recognized' })
    expect(saved).not.toHaveBeenCalled()
    resolve(turn)
    await Promise.resolve()
    expect(saved).toHaveBeenCalledTimes(1)
    expect(saved.mock.calls[0][0].detail.artifactId).toBe('recaptured-page')
  })
  it('fails without a listener and never invents a completed receipt', async () => {
    vi.useFakeTimers()
    const complete = vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue(turn)
    const receipts = bridge()
    receipts.request(turn, false, () => {})
    expect(complete).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(250)
    expect(complete).toHaveBeenCalledWith('turn-camera', expect.any(String), 'failed', 'DEVICE_CAPTURE_NO_LISTENER', expect.any(Object))
  })

  it('keeps accepted and captured pending until correlated OCR evidence arrives', async () => {
    const complete = vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue(turn)
    const receipts = bridge()
    receipts.request(turn, true, (request) => request.accept())
    emitCaptureReceipt({ sessionId: 'session-b', commandId: 'turn-camera', status: 'recognized', artifactId: 'other-child' })
    emitCaptureReceipt({ sessionId: 'session-a', commandId: 'turn-camera', status: 'captured' })
    expect(complete).not.toHaveBeenCalled()
    emitCaptureReceipt({ sessionId: 'session-a', commandId: 'turn-camera', status: 'recognized', artifactId: 'artifact-a' })
    await Promise.resolve()
    expect(complete).toHaveBeenCalledTimes(1)
    expect(complete).toHaveBeenCalledWith('turn-camera', expect.any(String), 'completed', undefined, expect.objectContaining({
      client_bridge_receipt: expect.objectContaining({ output: expect.objectContaining({ capture_artifact_id: 'artifact-a', artifact_id: 'artifact-a', device_status: 'recognized', ocr: { status: 'completed', artifact_id: 'artifact-a', error_code: null } }) }),
    }))
    emitCaptureReceipt({ sessionId: 'session-a', commandId: 'turn-camera', status: 'recognized', artifactId: 'duplicate' })
    expect(complete).toHaveBeenCalledTimes(1)
  })

  it.each(['failed', 'cancelled'] as const)('writes %s, not completed, after acceptance', async (status) => {
    const complete = vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue(turn)
    const receipts = bridge()
    receipts.request(turn, false, (request) => request.accept())
    emitCaptureReceipt({ sessionId: 'session-a', commandId: 'turn-camera', status, errorCode: status === 'failed' ? 'OCR_FAILED' : 'DEVICE_CAPTURE_CANCELLED', ...(status === 'failed' ? { ocrStatus: 'failed' as const } : {}) })
    await Promise.resolve()
    expect(complete.mock.calls[0][2]).toBe('failed')
    expect(complete.mock.calls[0][4]?.client_bridge_receipt).toMatchObject({ output: { device_status: status } })
    expect(complete.mock.calls[0][4]?.execution_receipt).toMatchObject({ output: { ocr: { status: status === 'failed' ? 'failed' : 'not_completed' } } })
  })

  it('replays an unsaved final receipt after restart without replaying capture', async () => {
    const complete = vi.spyOn(localApi, 'completeAgentTurn').mockRejectedValueOnce(new Error('offline')).mockResolvedValue(turn)
    const first = bridge()
    first.request(turn, false, (request) => request.accept())
    emitCaptureReceipt({ sessionId: 'session-a', commandId: 'turn-camera', status: 'recognized', artifactId: 'artifact-kept' })
    await Promise.resolve()
    expect(first.onError).toHaveBeenCalledWith(expect.stringContaining('尚未同步保存'))
    cleanups.pop()!()
    bridge()
    await Promise.resolve()
    expect(complete).toHaveBeenCalledTimes(2)
    expect(complete.mock.calls[1][4]?.execution_receipt).toMatchObject({ output: { artifact_id: 'artifact-kept', device_status: 'recognized' } })
    expect(Object.keys(localStorage)).toHaveLength(0)
  })

  it('marks an interrupted accepted request cancelled on restart', async () => {
    const complete = vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue(turn)
    bridge().request(turn, false, (request) => request.accept())
    cleanups.pop()!()
    bridge()
    await Promise.resolve()
    expect(complete).toHaveBeenCalledWith('turn-camera', expect.any(String), 'failed', 'DEVICE_CAPTURE_INTERRUPTED', expect.any(Object))
  })

  it('does not dispatch a physical action when its association cannot be saved', () => {
    const receipt = bridge()
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('storage full') })
    const dispatch = vi.fn()
    expect(() => receipt.request(turn, false, dispatch)).toThrow('storage full')
    expect(dispatch).not.toHaveBeenCalled()
  })
})

describe('privacy preemption contract', () => {
  it.each(['停止全部监听', '关闭摄像头和麦克风。', '别听了'])('recognizes %s locally', (text) => {
    expect(isImmediatePrivacyStop(text)).toBe(true)
    expect(isImmediatePrivacyStop(`不要${text}`)).toBe(false)
  })
  it('rejects late API results and refuses subsequent actions after invalidation', async () => {
    let resolve!: (value: string) => void
    let current = true
    const api = { load: vi.fn(() => new Promise<string>((done) => { resolve = done })) }
    const guarded = guardCommandApi(api, () => current)
    const promise = guarded.load()
    current = false
    resolve('late')
    await expect(promise).rejects.toBeInstanceOf(SupersededDeviceCommand)
    await expect(guarded.load()).rejects.toBeInstanceOf(SupersededDeviceCommand)
    expect(api.load).toHaveBeenCalledTimes(1)
  })
})
