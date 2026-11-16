import { afterEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { clearVoiceCaptureContext, setVoiceQuestionCapture, trackDiagnosticCapture } from './voiceCaptureContext'
import type { DiagnosticPromptResult } from './types'
import { registerOriginalAudioCaptureReadiness, originalAudioPromptPlayback } from './originalAudioPromptPlayback'
import { SpeechArbiter } from './speechArbiter'

const oral = () => ({ status: 'waiting_answer', prompt: { prompt_id: 'p', item_version: 'v',
  child_id: 'a', origin_session_id: 's', item: { track: 'pet', answer_type: 'speaking',
    assessment_mode: 'pet_reference_rubric' } } }) as DiagnosticPromptResult
const owners: Array<ReturnType<typeof registerOriginalAudioCaptureReadiness>> = []
afterEach(() => { owners.splice(0).forEach(owner => owner.dispose()); clearVoiceCaptureContext(); vi.restoreAllMocks(); vi.useRealTimers() })
const owner = () => { const value = registerOriginalAudioCaptureReadiness('s', 'a'); owners.push(value); return value }
const ready = { runtime: 'segmented_local' as const, phase: 'listening' as const, captureReady: true,
  speechActive: false, capturePending: false, pendingUtterances: 0, recoverable: true }
async function prepare() {
  await trackDiagnosticCapture('s', async () => oral())
  vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue(oral())
  return originalAudioPromptPlayback('s', oral())
}

it('waits for actual PCM readiness and drained old tool before reading and playing the original', async () => {
  const policy = await prepare(), capture = owner(), settled = vi.fn()
  const admission = policy.beforePlayback!(new AbortController().signal).then(settled)
  capture.update({ ...ready, captureReady: false }, false)
  await Promise.resolve(); expect(settled).not.toHaveBeenCalled()
  capture.update(ready, true)
  await Promise.resolve(); expect(settled).not.toHaveBeenCalled()
  capture.update(ready, false)
  await admission
  expect(settled).toHaveBeenCalledWith(true)
  expect(localApi.diagnosticPrompt).toHaveBeenCalledExactlyOnceWith('s', 'p')
})

it.each(['connecting', 'error', 'wake_standby', 'transcribing'] as const)('does not treat %s as conversation capture readiness', async phase => {
  const policy = await prepare(), capture = owner(), controller = new AbortController(), settled = vi.fn()
  const admission = policy.beforePlayback!(controller.signal).then(settled)
  capture.update({ ...ready, phase }, false)
  await Promise.resolve(); expect(settled).not.toHaveBeenCalled()
  controller.abort(); await admission; expect(settled).toHaveBeenCalledWith(false)
  expect(localApi.diagnosticPrompt).not.toHaveBeenCalled()
})

it('drops a cancelled or replaced source while waiting without querying or resurrecting it', async () => {
  const policy = await prepare(); owner()
  const admission = policy.beforePlayback!(new AbortController().signal)
  setVoiceQuestionCapture('s', 'a', { kind: 'diagnostic', binding: { prompt_id: 'new', item_version: 'new-v' } })
  expect(await admission).toBe(false); expect(localApi.diagnosticPrompt).not.toHaveBeenCalled()
})

it('rechecks server cancellation and capture loss during the read', async () => {
  const policy = await prepare(), capture = owner(); capture.update(ready, false)
  let finish!: (value: DiagnosticPromptResult) => void
  vi.mocked(localApi.diagnosticPrompt).mockImplementation(() => new Promise(resolve => { finish = resolve }))
  const admission = policy.beforePlayback!(new AbortController().signal)
  await vi.waitFor(() => expect(finish).toBeDefined())
  capture.dispose(); finish(oral()); expect(await admission).toBe(false)
})

it('rejects a saved answer, reason, version or child mismatch returned by the server', async () => {
  const policy = await prepare(), capture = owner(); capture.update(ready, false)
  for (const value of [{ ...oral(), status: 'answer_saved' }, { ...oral(), reason: 'revoked' },
    { ...oral(), prompt: { ...oral().prompt!, item_version: 'different' } },
    { ...oral(), prompt: { ...oral().prompt!, child_id: 'b' } }]) {
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(value as DiagnosticPromptResult)
    expect(await policy.beforePlayback!(new AbortController().signal)).toBe(false)
  }
})

it('leaves ordinary and non-original prompts ungated and preserves upstream playback checks', async () => {
  expect(originalAudioPromptPlayback('s', { ...oral(), status: 'answered' })).toEqual({})
  const before = vi.fn().mockResolvedValue(false), policy = await prepare(), capture = owner()
  capture.update(ready, false)
  const gated = originalAudioPromptPlayback('s', oral(), { beforePlayback: before, isCurrent: () => true })
  expect(await gated.beforePlayback!(new AbortController().signal)).toBe(false)
  expect(before).toHaveBeenCalledTimes(1); expect(localApi.diagnosticPrompt).not.toHaveBeenCalled()
  expect(policy.localOnly).toBe(true)
})

it('snapshots upstream options before the Runtime merges them back into its policy', async () => {
  await prepare(); const capture = owner(); capture.update(ready, false)
  const options = { localOnly: true, isCurrent: () => true }
  Object.assign(options, originalAudioPromptPlayback('s', oral(), options))
  expect(options.isCurrent()).toBe(true)
  expect(await (options as ReturnType<typeof originalAudioPromptPlayback>).beforePlayback!(new AbortController().signal)).toBe(true)
})

it('does not hand an old playback lease to a replaced host or another child', async () => {
  const policy = await prepare(); owner()
  const admission = policy.beforePlayback!(new AbortController().signal)
  const replacement = registerOriginalAudioCaptureReadiness('s', 'b'); owners.push(replacement)
  replacement.update(ready, false)
  expect(await admission).toBe(false)
  expect(localApi.diagnosticPrompt).not.toHaveBeenCalled()
})

it('uses the existing preparation timeout rather than playing without PCM and permits a newly re-read original to recover', async () => {
  vi.useFakeTimers()
  const policy = await prepare(), capture = owner(), played = vi.fn(), settled = vi.fn(), arbiter = new SpeechArbiter(() => undefined)
  arbiter.submit({ scope: 's', beforePlay: policy.beforePlayback, isCurrent: policy.isCurrent, play: played, settled })
  await vi.advanceTimersByTimeAsync(10_001)
  expect(played).not.toHaveBeenCalled(); expect(settled).toHaveBeenCalledWith('unavailable')
  expect(localApi.diagnosticPrompt).not.toHaveBeenCalled()
  capture.update(ready, false)
  arbiter.submit({ scope: 's', beforePlay: policy.beforePlayback, isCurrent: policy.isCurrent, play: finish => { played(); finish() } })
  await vi.advanceTimersByTimeAsync(1)
  expect(played).toHaveBeenCalledTimes(1)
  arbiter.cancel()
})

it('rejects a source withdrawn while its final server read was in flight', async () => {
  const policy = await prepare(), capture = owner(); capture.update(ready, false)
  let finish!: (value: DiagnosticPromptResult) => void
  vi.mocked(localApi.diagnosticPrompt).mockImplementation(() => new Promise(resolve => { finish = resolve }))
  const admission = policy.beforePlayback!(new AbortController().signal)
  await vi.waitFor(() => expect(finish).toBeDefined())
  clearVoiceCaptureContext('s'); finish(oral())
  expect(await admission).toBe(false)
})

it('activates a restored original conversation once instead of requiring the child to wake it again', async () => {
  const policy = await prepare(), activate = vi.fn()
  const capture = registerOriginalAudioCaptureReadiness('s', 'a', activate); owners.push(capture)
  capture.update({ ...ready, phase: 'wake_standby' }, false)
  const admission = policy.beforePlayback!(new AbortController().signal)
  expect(activate).toHaveBeenCalledTimes(1)
  capture.update({ ...ready, captureReady: false }, false)
  expect(activate).toHaveBeenCalledTimes(1)
  capture.update(ready, false)
  expect(await admission).toBe(true)
})
