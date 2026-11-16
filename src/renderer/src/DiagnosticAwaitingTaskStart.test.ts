import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'
import { runDiagnosticVoice } from './diagnosticVoiceFlow'
import { clearVoiceCaptureContext, setVoiceQuestionCapture, trackDiagnosticCapture, voiceCaptureReference } from './voiceCaptureContext'
import type { DiagnosticPromptResult, SessionWorkspaceState } from './types'

const waiting = { status: 'waiting_answer', prompt: {
  prompt_id: 'original', item_version: 'v1', diagnostic_item_id: 'pet-original', child_id: 'child-a',
  origin_session_id: 'session-a', review_task_id: null, created_at: '2026-09-08T10:00:00Z',
  item: { diagnostic_item_id: 'pet-original', track: 'pet', answer_type: 'listening_text',
    pet_audio_modality: 'listening', prompt: 'Where?', stimulus_text: 'Do not speak the original transcript.' },
}, attempt: null, reason: null } as DiagnosticPromptResult
const awaiting: DiagnosticPromptResult = { ...waiting, status: 'awaiting_task_start', reason: 'scheduled_pet_task_not_started', plan_item_id: 'original-plan-item' }
const workspace = { session: { session_id: 'session-a', child_id: 'child-a', state: 'study' }, plan_items: [] } as unknown as SessionWorkspaceState
const bind = (id = 'original', child = 'child-a', version = 'v1') => setVoiceQuestionCapture('session-a', child,
  { kind: 'diagnostic', binding: { prompt_id: id, item_version: version } })
beforeEach(() => { vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('NO_NETWORK'))) })
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); clearVoiceCaptureContext(); resetRuntimeCache() })

it.each(['start_diagnostic', 'replay_diagnostic', 'submit_diagnostic_answer', 'cancel_diagnostic'] as const)(
  '%s preserves the awaiting original and directs task recovery without preparation, grading, or cancellation', async type => {
    vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue(awaiting)
    const admit = vi.spyOn(localApi, 'admitOriginalClientExecution').mockResolvedValue({ status: 'admitted' })
    const prepare = vi.spyOn(localApi, 'prepareDiagnosticPrompt').mockResolvedValue(waiting)
    const answer = vi.spyOn(localApi, 'answerDiagnosticPrompt').mockResolvedValue(waiting)
    const resume = vi.spyOn(localApi, 'resumeDiagnosticPrompt').mockResolvedValue(waiting)
    const cancel = vi.spyOn(localApi, 'cancelDiagnosticPrompt').mockResolvedValue(waiting)
    bind()
    const result = await runDiagnosticVoice(workspace, { type, track: 'pet', answer: 'library',
      diagnostic_prompt_id: 'original', diagnostic_item_version: 'v1' }, 'original-turn', () => true)
    expect(result.result).toBe(awaiting)
    expect(result.response).toMatch(/继续.*原任务/)
    expect(result.response).not.toContain(waiting.prompt!.item.stimulus_text)
    expect(prepare).not.toHaveBeenCalled(); expect(answer).not.toHaveBeenCalled()
    expect(admit).not.toHaveBeenCalled()
    expect(resume).not.toHaveBeenCalled(); expect(cancel).not.toHaveBeenCalled()
    expect(voiceCaptureReference('session-a', 'child-a').question_reference).toBeNull()
  })

it.each(['other_prompt', 'other_child', 'new_version', 'other_session'] as const)(
  'awaiting GET cannot clear %s capture', async scope => {
    bind(scope === 'other_prompt' ? 'new-original' : 'original', scope === 'other_child' ? 'child-b' : 'child-a', scope === 'new_version' ? 'v2' : 'v1')
    const child = scope === 'other_child' ? 'child-b' : 'child-a'
    const before = voiceCaptureReference('session-a', child)
    const result = scope === 'other_session' ? { ...awaiting, prompt: { ...awaiting.prompt!, origin_session_id: 'session-b' } } : awaiting
    await trackDiagnosticCapture('session-a', async () => result)
    expect(voiceCaptureReference('session-a', child)).toEqual(before)
  })

it('clears only the exact stale capture and preserves a segment already captured before the read', async () => {
  bind()
  const captured = voiceCaptureReference('session-a', 'child-a')
  await trackDiagnosticCapture('session-a', async () => awaiting)
  expect(voiceCaptureReference('session-a', 'child-a').question_reference).toBeNull()
  expect(captured.question_reference).toMatchObject({ binding: { prompt_id: 'original', item_version: 'v1' } })
  await trackDiagnosticCapture('session-a', async () => waiting)
  expect(voiceCaptureReference('session-a', 'child-a')).toEqual(captured)
})

it('ignores late awaiting GET after another prompt has taken capture ownership', async () => {
  bind()
  let resolve!: (value: DiagnosticPromptResult) => void
  const read = trackDiagnosticCapture('session-a', () => new Promise(done => { resolve = done }))
  bind('new-original')
  resolve(awaiting); await read
  expect(voiceCaptureReference('session-a', 'child-a').question_reference).toMatchObject({ binding: { prompt_id: 'new-original' } })
})

it.each(['next', 'answer'] as const)('legacy %s wrapper preserves awaiting state instead of preparing/submitting', async operation => {
  vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue(awaiting)
  const prepare = vi.spyOn(localApi, 'prepareDiagnosticPrompt').mockResolvedValue(waiting)
  const answer = vi.spyOn(localApi, 'answerDiagnosticPrompt').mockResolvedValue(waiting)
  const result = operation === 'next' ? localApi.nextDiagnostic('session-a', 'pet', 'pet-original')
    : localApi.answerDiagnostic('session-a', 'pet-original', 'library')
  await expect(result).rejects.toMatchObject({ result: awaiting, message: expect.stringMatching(/继续.*原任务/) })
  expect(prepare).not.toHaveBeenCalled(); expect(answer).not.toHaveBeenCalled()
})

it.each(['prepare', 'resume', 'answer'] as const)('preserves awaiting returned by %s after the initial read', async phase => {
  const first: DiagnosticPromptResult = phase === 'prepare' ? { ...waiting, status: 'no_item', prompt: null }
    : phase === 'resume' ? { ...waiting, status: 'answer_saved' } : waiting
  const read = vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue(first)
  const admit = vi.spyOn(localApi, 'admitOriginalClientExecution').mockResolvedValue({ status: 'admitted' })
  vi.spyOn(localApi, 'prepareDiagnosticPrompt').mockResolvedValue(awaiting)
  vi.spyOn(localApi, 'resumeDiagnosticPrompt').mockResolvedValue(awaiting)
  vi.spyOn(localApi, 'answerDiagnosticPrompt').mockResolvedValue(awaiting)
  const result = await runDiagnosticVoice(workspace, phase === 'answer'
    ? { type: 'submit_diagnostic_answer', answer: 'library', diagnostic_prompt_id: 'original', diagnostic_item_version: 'v1' }
    : { type: 'start_diagnostic', track: 'pet' }, 'old-turn', () => true)
  expect(result.result).toBe(awaiting)
  expect(result.result.attempt).toBeNull()
  expect(result.response).toMatch(/继续.*原任务/)
  if (phase === 'prepare') {
    expect(admit).toHaveBeenCalledExactlyOnceWith('session-a', 'old-turn')
    read.mockResolvedValue(awaiting)
    expect((await runDiagnosticVoice(workspace, { type: 'start_diagnostic', track: 'pet' }, 'recover-turn', () => true)).result).toBe(awaiting)
    expect(admit).toHaveBeenCalledTimes(1)
  } else expect(admit).not.toHaveBeenCalled()
})

it('actual request adapter reads awaiting without any prepare/answer POST, then binds the same original once started', async () => {
  resetRuntimeCache()
  vi.stubGlobal('desktop', { getRuntimeConfig: async () => ({ status: 'ready', localApiUrl: 'http://127.0.0.1:19089', sessionToken: 'synthetic-session' }) })
  const fetcher = vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify(awaiting)))
    .mockResolvedValueOnce(new Response(JSON.stringify(awaiting)))
    .mockResolvedValueOnce(new Response(JSON.stringify(waiting)))
  bind()
  expect(await localApi.diagnosticPrompt('session-a')).toEqual(awaiting)
  expect(voiceCaptureReference('session-a', 'child-a').question_reference).toBeNull()
  await expect(localApi.nextDiagnostic('session-a', 'pet')).rejects.toMatchObject({ result: awaiting })
  expect((await localApi.nextDiagnostic('session-a', 'pet')).item).toEqual(waiting.prompt!.item)
  expect(voiceCaptureReference('session-a', 'child-a').question_reference).toMatchObject({ binding: { prompt_id: 'original', item_version: 'v1' } })
  expect(fetcher.mock.calls).toHaveLength(3)
  for (const [url, init] of fetcher.mock.calls) {
    expect(url).toBe('http://127.0.0.1:19089/v1/sessions/session-a/diagnostic-prompt')
    expect(init?.method ?? 'GET').toBe('GET')
  }
})
