import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import * as mediaDevices from './mediaDevices'
import { cancelLocalSpeech, dispatchHandsFreeTranscript, HandsFreeVoiceInput, VoiceInputButton } from './LocalInputs'
import { clearVoiceCaptureContext, setVoiceLocalRecoveryCapture, setVoicePendingEvaluationCapture } from './voiceCaptureContext'
import { resetVoiceCaptureCoordinatorForTests } from './voiceCaptureCoordinator'
import { invalidateCarryoverReview, markCarryoverReadback, prepareCarryoverReview, readCarryoverReview } from './pendingCarryoverReview'
import type { DeferredTaskCandidate, VoiceInput } from './types'

const scope = { sessionId: 'capture-session', childId: 'capture-child' }
const originalMedia = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
let processor: { onaudioprocess: ((event: AudioProcessingEvent) => void) | null }
let resolveAsr: (value: VoiceInput) => void
const delivered = vi.fn()

beforeEach(() => {
  vi.useFakeTimers(); localStorage.clear(); delivered.mockReset()
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('NETWORK_FORBIDDEN')))
  vi.stubGlobal('AudioContext', class {
    sampleRate = 16000
    createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
    createScriptProcessor = () => {
      processor = { onaudioprocess: null }
      return Object.assign(processor, { connect: vi.fn(), disconnect: vi.fn() })
    }
    close = vi.fn().mockResolvedValue(undefined)
  })
  const track = Object.assign(new EventTarget(), { stop: vi.fn(), getSettings: () => ({}), getCapabilities: () => ({}) })
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
    getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track], getAudioTracks: () => [track] }),
    enumerateDevices: vi.fn().mockResolvedValue([]),
  } })
  vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'capture-mic', status: 'active', expires_at: 'later' })
  // Device identity is synthetic; native crypto scheduling is not this capture-token contract.
  vi.spyOn(mediaDevices, 'mediaDeviceRegistration').mockResolvedValue({ systemIdHash: 'd'.repeat(64), capabilities: {} })
  vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'capture-mic', status: 'released' })
  vi.spyOn(localApi, 'transcribe').mockImplementation(() => new Promise(resolve => { resolveAsr = resolve }))
})
afterEach(() => {
  cleanup(); cancelLocalSpeech(); clearVoiceCaptureContext(); resetVoiceCaptureCoordinatorForTests()
  vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); localStorage.clear()
  if (originalMedia) Object.defineProperty(navigator, 'mediaDevices', originalMedia)
  else Reflect.deleteProperty(navigator, 'mediaDevices')
})

function pending() {
  const candidate = { source_plan_item_id: 'original-task', source_version: 'a'.repeat(64), subject: '数学', title: '原作业' } as DeferredTaskCandidate
  const request = prepareCarryoverReview(scope, candidate, 'continue_today', '孩子确认', 'original-turn')
  invalidateCarryoverReview(request, { not_applied: true, session_id: scope.sessionId, child_id: scope.childId,
    client_action_id: request.requestId, candidate: { ...candidate, source_version: 'b'.repeat(64) } })
  return readCarryoverReview(scope.sessionId, scope.childId)!
}
async function mount(childId = scope.childId) {
  let view!: ReturnType<typeof render>
  await act(async () => { view = render(<HandsFreeVoiceInput sessionId={scope.sessionId} childId={childId}
    purpose="agent_command" autoStart onTranscript={delivered} />) })
  await act(async () => { await vi.waitFor(() => expect(localApi.registerDevice).toHaveBeenCalled()) })
  return view
}
async function utterance() {
  for (let i = 0; i < 4; i++) await act(async () => {
    await vi.advanceTimersByTimeAsync(256)
    processor.onaudioprocess?.({ inputBuffer: { getChannelData: () =>
      Float32Array.from({ length: 4096 }, (_, j) => Math.sin(j * Math.PI / 20) * .05) } } as unknown as AudioProcessingEvent)
  })
}
async function endpoint() { await act(async () => { await vi.advanceTimersByTimeAsync(1200) }) }
function result(id = 'voice-1', mode = 'ordinary'): VoiceInput {
  return { voice_input_id: id, transcript: '今天继续这道作业', status: 'completed', duration_ms: 1024,
    model_version: 'synthetic-test', cloud_used: mode !== 'recovery', local_path: null,
    capture_binding: { status: 'completed', prompt_id: null, item_version: null, raw_audio_available: false,
      ...(mode === 'pending' ? { pending_evaluation_turn_id: 'evaluation-original' } : {}),
      ...(mode === 'recovery' ? { local_recovery_only: true } : {}) } }
}

it('freezes explicit null at VAD before readback, including a second utterance queued behind delayed ASR', async () => {
  const request = pending()
  await mount(); await utterance(); await endpoint()
  await utterance(); await endpoint()
  expect(localApi.transcribe).toHaveBeenCalledTimes(1)
  markCarryoverReadback(request)
  await act(async () => { resolveAsr(result()) })
  expect(delivered.mock.calls[0]).toEqual(['今天继续这道作业', undefined, 'voice-1', undefined, undefined, null])
  expect(localApi.transcribe).toHaveBeenCalledTimes(2)
  await act(async () => { resolveAsr(result('voice-2')) })
  expect(delivered.mock.calls[1]?.[5]).toBeNull()
  await utterance(); await endpoint()
  await act(async () => { resolveAsr(result('voice-3')) })
  expect(delivered.mock.calls[2]?.[5]).toBe(request.confirmationId)
  // The token is local callback metadata, never an ASR capture reference field.
  for (const call of vi.mocked(localApi.transcribe).mock.calls) {
    expect(Object.keys(call[5] ?? {}).sort()).toEqual(['child_id', 'question_reference'])
    expect(JSON.stringify(call[5])).not.toContain(request.confirmationId!)
  }
  expect(fetch).not.toHaveBeenCalled()
})

it.each(['pending', 'recovery'] as const)('preserves frozen %s identity without borrowing changed capture context', async mode => {
  const request = pending(); markCarryoverReadback(request)
  setVoicePendingEvaluationCapture(scope.sessionId, scope.childId, 'evaluation-original')
  setVoiceLocalRecoveryCapture(scope.sessionId, scope.childId, mode === 'recovery')
  await mount(); await utterance(); await endpoint()
  setVoicePendingEvaluationCapture(scope.sessionId, scope.childId, 'evaluation-later')
  setVoiceLocalRecoveryCapture(scope.sessionId, scope.childId, mode !== 'recovery')
  await act(async () => { resolveAsr(result('voice-mode', mode)) })
  expect(delivered).toHaveBeenCalledExactlyOnceWith('今天继续这道作业', undefined, 'voice-mode',
    mode === 'pending' ? 'evaluation-original' : undefined, mode === 'recovery' ? true : undefined, request.confirmationId)
  expect(localApi.transcribe).toHaveBeenCalledTimes(1)
  expect(fetch).not.toHaveBeenCalled()
})

it('another child cannot capture the stored confirmation', async () => {
  const request = pending(); markCarryoverReadback(request)
  await mount('another-child'); await utterance(); await endpoint()
  await act(async () => { resolveAsr(result()) })
  expect(delivered.mock.calls[0]?.[5]).toBeNull()
})

it('manual capture freezes at recording start, not stop or ASR completion', async () => {
  const request = pending()
  render(<VoiceInputButton sessionId={scope.sessionId} childId={scope.childId} purpose="agent_command" onTranscript={delivered} />)
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始说话' })) })
  await act(async () => { await vi.waitFor(() => expect(screen.getByRole('button', { name: '结束并识别' })).toBeInTheDocument()) })
  await utterance()
  markCarryoverReadback(request)
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '结束并识别' })) })
  await act(async () => { resolveAsr(result()) })
  expect(delivered.mock.calls[0]?.[5]).toBeNull()
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始说话' })) })
  await act(async () => { await vi.waitFor(() => expect(screen.getByRole('button', { name: '结束并识别' })).toBeInTheDocument()) })
  await utterance()
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '结束并识别' })) })
  await act(async () => { resolveAsr(result('voice-new')) })
  expect(delivered.mock.calls[1]?.[5]).toBe(request.confirmationId)
  expect(fetch).not.toHaveBeenCalled()
})

it('a restored voice receipt with no capture token dispatches explicit null despite ready storage', () => {
  const request = pending(); markCarryoverReadback(request)
  dispatchHandsFreeTranscript(() => true, delivered, '今天继续', undefined, undefined, 'old-voice')
  expect(delivered).toHaveBeenCalledExactlyOnceWith('今天继续', undefined, 'old-voice', undefined, undefined, null)
})
