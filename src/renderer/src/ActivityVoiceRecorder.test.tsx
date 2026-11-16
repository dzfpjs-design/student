import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { HandsFreeVoiceInput } from './LocalInputs'
import { localApi } from './api'
import { clearVoiceCaptureContext, setVoiceActivityCapture, setVoiceQuestionCapture, setVoicePendingEvaluationCapture, setVoiceLocalRecoveryCapture } from './voiceCaptureContext'
import { resetVoiceCaptureCoordinatorForTests } from './voiceCaptureCoordinator'

const originalMedia = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
afterEach(() => {
  cleanup(); clearVoiceCaptureContext(); resetVoiceCaptureCoordinatorForTests()
  vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers()
  if (originalMedia) Object.defineProperty(navigator, 'mediaDevices', originalMedia)
  else Reflect.deleteProperty(navigator, 'mediaDevices')
})

it.each(['ordinary', 'pending', 'mismatched', 'local-recovery'] as const)('freezes references at speech onset, not segment end or delayed FIFO transmission (%s)', async mode => {
  vi.useFakeTimers()
  const processor = { connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null as ((event: AudioProcessingEvent) => void) | null }
  vi.stubGlobal('AudioContext', class {
    sampleRate = 16000
    createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
    createScriptProcessor = () => processor
    close = vi.fn().mockResolvedValue(undefined)
  })
  const track = { stop: vi.fn(), getSettings: () => ({}), getCapabilities: () => ({}) }
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
    getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track], getAudioTracks: () => [track] }),
    enumerateDevices: vi.fn().mockResolvedValue([]),
  } })
  vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'activity-test', status: 'active', expires_at: 'later' })
  vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'activity-test', status: 'released' })
  const pending: Array<(value: Awaited<ReturnType<typeof localApi.transcribe>>) => void> = []
  const transcribe = vi.spyOn(localApi, 'transcribe').mockImplementation(() => new Promise(resolve => pending.push(resolve)))
  const focus = { activity_id: 'activity-a', version: 'v1', last_turn_id: 'turn-1' }
  const second = { ...focus, version: 'v2', last_turn_id: 'turn-2' }
  const question = { kind: 'diagnostic' as const, binding: { prompt_id: 'question-a', item_version: 'item-v1' } }
  setVoiceQuestionCapture('session-a', 'child-a', question)
  setVoiceActivityCapture('session-a', 'child-a', focus)
  if (mode !== 'ordinary') setVoicePendingEvaluationCapture('session-a', 'child-a', 'evaluation-one')
  const localRecovery = mode === 'local-recovery'
  setVoiceLocalRecoveryCapture('session-a', 'child-a', localRecovery)
  const delivered = vi.fn()
  render(<HandsFreeVoiceInput sessionId="session-a" childId="child-a" purpose={mode === 'ordinary' ? 'agent_command' : 'pet_speaking'} onTranscript={delivered} />)
  fireEvent.click(screen.getByRole('button', { name: '免按键对话' }))
  await act(async () => { await vi.advanceTimersByTimeAsync(1) })
  const speakFrames = async () => {
    for (let n = 0; n < 4; n++) await act(async () => {
      await vi.advanceTimersByTimeAsync(256)
      processor.onaudioprocess?.({ inputBuffer: { getChannelData: () => new Float32Array(4096).fill(.05) } } as unknown as AudioProcessingEvent)
    })
  }
  await speakFrames()
  setVoiceLocalRecoveryCapture('session-a', 'child-a', !localRecovery)
  setVoiceActivityCapture('session-a', 'child-a', second)
  if (mode !== 'ordinary') setVoicePendingEvaluationCapture('session-a', 'child-a', 'evaluation-two')
  await act(async () => { await vi.advanceTimersByTimeAsync(1200) })
  expect(transcribe).toHaveBeenCalledTimes(1)
  expect(transcribe.mock.calls[0][1]).toBe('agent_command')
  expect(transcribe.mock.calls[0][5]).toEqual(localRecovery ? { child_id: 'child-a', local_recovery_only: true }
    : { child_id: 'child-a', question_reference: question, activity_reference: focus,
      ...(mode !== 'ordinary' ? { pending_evaluation_turn_id: 'evaluation-one' } : {}) })
  setVoiceLocalRecoveryCapture('session-a', 'child-a', localRecovery)
  await speakFrames()
  setVoiceLocalRecoveryCapture('session-a', 'child-a', !localRecovery)
  setVoiceActivityCapture('session-a', 'child-a', null)
  setVoicePendingEvaluationCapture('session-a', 'child-a', null)
  await act(async () => { await vi.advanceTimersByTimeAsync(1200) })
  expect(transcribe).toHaveBeenCalledTimes(1)
  await act(async () => {
    pending[0]({ voice_input_id: 'cancelled-capture', status: 'ignored', transcript: '', cloud_used: false,
      duration_ms: 1024, model_version: 'synthetic-local', local_path: null })
    await vi.advanceTimersByTimeAsync(1)
  })
  expect(transcribe).toHaveBeenCalledTimes(2)
  expect(transcribe.mock.calls[1][5]).toEqual(localRecovery ? { child_id: 'child-a', local_recovery_only: true }
    : { child_id: 'child-a', question_reference: question, activity_reference: second,
      ...(mode !== 'ordinary' ? { pending_evaluation_turn_id: 'evaluation-two' } : {}) })
  await act(async () => {
    pending[1]({ voice_input_id: 'saved-capture', status: 'completed', transcript: '请暂停一下', cloud_used: false,
      duration_ms: 1024, model_version: 'synthetic-local', local_path: null,
      capture_binding: localRecovery ? { status: 'completed', local_recovery_only: true, prompt_id: null, item_version: null, raw_audio_available: false }
        : { status: 'completed', prompt_id: question.binding.prompt_id, item_version: question.binding.item_version,
        raw_audio_available: false, ...(mode !== 'ordinary' ? { pending_evaluation_turn_id: mode === 'mismatched' ? 'wrong-evaluation' : 'evaluation-two' } : {}) } })
    await vi.advanceTimersByTimeAsync(1)
  })
  if (mode === 'mismatched') expect(delivered).not.toHaveBeenCalled()
  else if (localRecovery) expect(delivered).toHaveBeenCalledWith('请暂停一下', undefined, 'saved-capture', undefined, true, null)
  else if (mode === 'pending') expect(delivered).toHaveBeenCalledWith('请暂停一下', undefined, 'saved-capture', 'evaluation-two', undefined, null)
  else expect(delivered).toHaveBeenCalledWith('请暂停一下', undefined, 'saved-capture', undefined, undefined, null)
})
