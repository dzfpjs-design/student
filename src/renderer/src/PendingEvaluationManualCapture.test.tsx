import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { VoiceInputButton } from './LocalInputs'
import { localApi } from './api'
import { clearVoiceCaptureContext, setVoicePendingEvaluationCapture, setVoiceQuestionCapture, setVoiceLocalRecoveryCapture } from './voiceCaptureContext'
import { resetVoiceCaptureCoordinatorForTests } from './voiceCaptureCoordinator'

const originalMedia = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
afterEach(() => {
  cleanup(); clearVoiceCaptureContext(); resetVoiceCaptureCoordinatorForTests(); vi.restoreAllMocks(); vi.unstubAllGlobals()
  if (originalMedia) Object.defineProperty(navigator, 'mediaDevices', originalMedia)
  else Reflect.deleteProperty(navigator, 'mediaDevices')
})

it.each(['matching', 'mismatched', 'local-recovery', 'local-answer-box'])('freezes pending identity before manual recording and guards %s ASR receipt', async mode => {
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
  vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'manual-pending', status: 'active', expires_at: 'later' })
  vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'manual-pending', status: 'released' })
  const recovery = mode.startsWith('local-')
  const transcribe = vi.spyOn(localApi, 'transcribe').mockResolvedValue({ voice_input_id: 'asr-a', transcript: '请暂停',
    status: 'completed', duration_ms: 1024, model_version: 'synthetic', cloud_used: false, local_path: null,
    capture_binding: recovery ? { status: 'completed', local_recovery_only: true, prompt_id: null, item_version: null, raw_audio_available: false }
      : { status: 'completed', prompt_id: 'prompt-a', item_version: 'v1', raw_audio_available: false,
      pending_evaluation_turn_id: mode === 'matching' ? 'evaluation-a' : 'evaluation-b' } })
  const delivered = vi.fn()
  setVoicePendingEvaluationCapture('session-a', 'child-a', 'evaluation-a')
  setVoiceQuestionCapture('session-a', 'child-a', { kind: 'diagnostic', binding: { prompt_id: 'prompt-a', item_version: 'v1' } })
  setVoiceLocalRecoveryCapture('session-a', 'child-a', recovery)
  render(<VoiceInputButton sessionId="session-a" childId="child-a" purpose={mode === 'local-recovery' ? 'agent_command' : 'pet_speaking'} onTranscript={delivered} />)
  fireEvent.click(screen.getByRole('button', { name: '开始说话' }))
  const stop = await screen.findByRole('button', { name: '结束并识别' })
  setVoicePendingEvaluationCapture('session-a', 'child-a', 'evaluation-b')
  setVoiceLocalRecoveryCapture('session-a', 'child-a', !recovery)
  for (let i = 0; i < 4; i++) processor.onaudioprocess?.({
    inputBuffer: { getChannelData: () => new Float32Array(4096).fill(.05) },
  } as unknown as AudioProcessingEvent)
  await act(async () => { fireEvent.click(stop) })
  await waitFor(() => expect(transcribe).toHaveBeenCalledTimes(1))
  expect(transcribe.mock.calls[0][1]).toBe('agent_command')
  expect(transcribe.mock.calls[0][5]).toMatchObject(recovery ? { child_id: 'child-a', local_recovery_only: true }
    : { pending_evaluation_turn_id: 'evaluation-a', question_reference: { kind: 'diagnostic', binding: { prompt_id: 'prompt-a', item_version: 'v1' } } })
  if (mode === 'matching') expect(delivered).toHaveBeenCalledWith('请暂停', undefined, 'asr-a', 'evaluation-a', undefined, null)
  else if (mode === 'local-recovery') expect(delivered).toHaveBeenCalledWith('请暂停', undefined, 'asr-a', undefined, true, null)
  else expect(delivered).not.toHaveBeenCalled()
})
