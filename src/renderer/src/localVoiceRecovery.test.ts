import { afterEach, expect, it, vi } from 'vitest'
import { ApiError, localApi } from './api'
import { transcribeCapturedVoice } from './localVoiceRecovery'
import type { VoiceInput } from './types'
import { clearVoiceCaptureContext, setVoiceLocalRecoveryCapture, setVoiceQuestionCapture, setVoiceActivityCapture, setVoicePendingEvaluationCapture, voiceCaptureReference } from './voiceCaptureContext'

afterEach(() => { clearVoiceCaptureContext(); vi.restoreAllMocks() })
const reference = { child_id: 'child-a', pending_evaluation_turn_id: 'original-eval',
  question_reference: { kind: 'diagnostic' as const, binding: { prompt_id: 'original', item_version: 'v1' } } }
const localResult = (): VoiceInput => ({ voice_input_id: 'local-recovery-asr', transcript: '请找大人',
  status: 'completed', cloud_used: false, model_version: 'local', duration_ms: 1000, local_path: null,
  capture_binding: { status: 'completed', local_recovery_only: true, prompt_id: null, item_version: null, raw_audio_available: false } })

it('uses a distinct deterministic recovery key and strips all original references on exact stale evaluation', async () => {
  const transcribe = vi.spyOn(localApi, 'transcribe')
    .mockRejectedValueOnce(new ApiError('stale', 409, 'VOICE_CAPTURE_PENDING_EVALUATION_CHANGED'))
    .mockResolvedValueOnce(localResult())
  await transcribeCapturedVoice('session-a', 'child-a', 'pet_speaking', 'synthetic-wav', undefined, 'original-asr', reference, () => true)
  expect(transcribe.mock.calls.map(call => [call[0], call[1], call[4], call[5]])).toEqual([
    ['session-a', 'agent_command', 'original-asr', reference],
    ['session-a', 'agent_command', 'original-asr:local-recovery-v1', { child_id: 'child-a', local_recovery_only: true }],
  ])
})

it.each(['VOICE_CAPTURE_SOURCE_CHANGED', 'VOICE_CAPTURE_SCOPE_CHANGED', 'ASR_MODEL_NOT_READY'])('never retries unrelated %s', async code => {
  const transcribe = vi.spyOn(localApi, 'transcribe').mockRejectedValue(new ApiError('rejected', 409, code))
  await expect(transcribeCapturedVoice('session-a', 'child-a', 'agent_command', 'wav', undefined, 'key', reference, () => true)).rejects.toThrow()
  expect(transcribe).toHaveBeenCalledTimes(1)
})

it('does not retry a stale recording after child/session ownership is superseded', async () => {
  let current = true
  const transcribe = vi.spyOn(localApi, 'transcribe').mockImplementation(async () => {
    current = false
    throw new ApiError('stale', 409, 'VOICE_CAPTURE_PENDING_EVALUATION_CHANGED')
  })
  await expect(transcribeCapturedVoice('session-a', 'child-a', 'agent_command', 'wav', undefined, 'key', reference, () => current)).rejects.toThrow()
  expect(transcribe).toHaveBeenCalledTimes(1)
})

it.each(['cloud', 'missing-marker', 'original-reference'])('rejects %s recovery receipt without reinterpretation', async mode => {
  const result = localResult()
  if (mode === 'cloud') result.cloud_used = true
  if (mode === 'missing-marker') delete result.capture_binding!.local_recovery_only
  if (mode === 'original-reference') result.capture_binding!.pending_evaluation_turn_id = 'original-eval'
  const transcribe = vi.spyOn(localApi, 'transcribe').mockResolvedValue(result)
  await expect(transcribeCapturedVoice('session-a', 'child-a', 'pet_speaking', 'wav', undefined, 'key',
    { child_id: 'child-a', local_recovery_only: true }, () => true)).rejects.toThrow()
  expect(transcribe).toHaveBeenCalledTimes(1)
})

it('keeps local recovery separate from all reference generations and other children', () => {
  setVoiceQuestionCapture('session-a', 'child-a', reference.question_reference)
  setVoicePendingEvaluationCapture('session-a', 'child-a', 'original-eval')
  const before = voiceCaptureReference('session-a', 'child-a')
  setVoiceLocalRecoveryCapture('session-a', 'child-a', true)
  setVoiceActivityCapture('session-a', 'child-a', { activity_id: 'story', version: 'v1', last_turn_id: 't1' })
  const onset = voiceCaptureReference('session-a', 'child-a')
  expect(onset).toEqual({ child_id: 'child-a', local_recovery_only: true })
  expect(before).not.toHaveProperty('local_recovery_only')
  expect(voiceCaptureReference('session-a', 'child-b')).toEqual({ child_id: 'child-b', question_reference: null })
  setVoiceLocalRecoveryCapture('session-a', 'child-b', false)
  expect(voiceCaptureReference('session-a', 'child-a')).toEqual(onset)
  setVoiceLocalRecoveryCapture('session-a', 'child-a', false)
  expect(voiceCaptureReference('session-a', 'child-a')).toMatchObject(before)
  expect(onset).not.toHaveProperty('pending_evaluation_turn_id')
})

it('preserves model-not-ready code and never falls back to a cloud or ordinary request', async () => {
  const transcribe = vi.spyOn(localApi, 'transcribe').mockRejectedValue(new ApiError('local unavailable', 503, 'ASR_MODEL_NOT_READY'))
  await expect(transcribeCapturedVoice('session-a', 'child-a', 'agent_command', 'wav', undefined, 'local-key',
    { child_id: 'child-a', local_recovery_only: true }, () => true)).rejects.toMatchObject({ code: 'ASR_MODEL_NOT_READY' })
  expect(transcribe).toHaveBeenCalledTimes(1)
  expect(transcribe.mock.calls[0][5]).toEqual({ child_id: 'child-a', local_recovery_only: true })
})

it('cancels the distinct recovery operation when recording ownership is aborted during local recognition', async () => {
  const controller = new AbortController()
  const cancel = vi.spyOn(localApi, 'cancelTranscriptionAction').mockResolvedValue({ client_action_id: 'local-key', status: 'cancelled' })
  vi.spyOn(localApi, 'transcribe').mockRejectedValueOnce(new ApiError('changed', 409, 'VOICE_CAPTURE_PENDING_EVALUATION_CHANGED'))
    .mockImplementationOnce(async () => { controller.abort(); throw new DOMException('aborted', 'AbortError') })
  await expect(transcribeCapturedVoice('session-a', 'child-a', 'agent_command', 'wav', controller.signal, 'local-key', reference, () => true)).rejects.toThrow()
  expect(cancel).toHaveBeenCalledExactlyOnceWith('session-a', 'local-key:local-recovery-v1')
})

it('never clears unrelated references to legitimize a mixed recovery request', async () => {
  const transcribe = vi.spyOn(localApi, 'transcribe')
  await expect(transcribeCapturedVoice('session-a', 'child-a', 'agent_command', 'wav', undefined, 'local-key',
    { ...reference, local_recovery_only: true }, () => true)).rejects.toThrow()
  expect(transcribe).not.toHaveBeenCalled()
})
