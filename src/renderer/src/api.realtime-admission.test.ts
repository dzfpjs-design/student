import { afterEach, expect, it, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'
import { pendingAgentRequests } from './agentRequestJournal'

const reference = { child_id: 'synthetic-child', question_reference: {
  kind: 'diagnostic' as const, binding: { prompt_id: 'original-prompt', item_version: 'original-v1' },
} }
const original = '小知，原题我选B。'
const receipt = { voice_input_id: 'synthetic-voice', client_action_id: 'synthetic-input', session_id: 'synthetic-session',
  child_id: reference.child_id, original_transcript: original, status: 'completed',
  source_kind: 'realtime_transcript', raw_audio_available: false }
const response = (body: unknown) => new Response(JSON.stringify(body))
afterEach(() => { localStorage.clear(); delete window.desktop; resetRuntimeCache(); vi.unstubAllGlobals(); vi.useRealTimers() })

it.each(['begin', 'complete'])('bounds a stalled %s admission and never executes or retries an unknown result', async phase => {
  vi.useFakeTimers()
  let finish!: (value: Response) => void
  const stalled = new Promise<Response>(resolve => { finish = resolve })
  const fetcher = phase === 'begin' ? vi.fn().mockReturnValue(stalled)
    : vi.fn().mockResolvedValueOnce(response({ status: 'prepared' })).mockReturnValue(stalled)
  vi.stubGlobal('fetch', fetcher)
  const outcome = localApi.admitRealtimeVoice('synthetic-session', 'synthetic-input', original, 100000, reference)
    .then(record => ({ record }), error => ({ error }))
  await vi.advanceTimersByTimeAsync(10_000)
  const marker = { unsettled: true }
  expect(await Promise.race([outcome, Promise.resolve(marker)])).toMatchObject({
    error: { code: 'REALTIME_CAPTURE_UNCONFIRMED' },
  })
  const calls = phase === 'begin' ? 1 : 2
  expect(fetcher).toHaveBeenCalledTimes(calls)
  expect(fetcher.mock.calls.at(-1)![1].signal.aborted).toBe(true)
  finish(response(phase === 'begin' ? { status: 'prepared' } : receipt))
  await vi.advanceTimersByTimeAsync(1)
  expect(fetcher).toHaveBeenCalledTimes(calls)
  expect(pendingAgentRequests('synthetic-session')).toEqual([])
})

it('does not begin an already cancelled input', async () => {
  const controller = new AbortController(); controller.abort()
  const fetcher = vi.fn().mockResolvedValue(response(receipt))
  vi.stubGlobal('fetch', fetcher)
  await expect(localApi.admitRealtimeVoice('synthetic-session', 'synthetic-input', original, 100000, reference, controller.signal))
    .rejects.toMatchObject({ name: 'AbortError' })
  expect(fetcher).not.toHaveBeenCalled()
})

it('sends original onset, source and complete ASR over the actual API adapter before Runtime', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(response({ status: 'prepared' })).mockResolvedValueOnce(response(receipt))
  vi.stubGlobal('fetch', fetcher)
  expect(await localApi.admitRealtimeVoice('synthetic-session', 'synthetic-input', original, 100000, reference)).toEqual(receipt)
  const begin = JSON.parse(fetcher.mock.calls[0][1].body)
  expect(begin).toEqual({ client_action_id: 'synthetic-input', captured_at_ms: 100000,
    model_label: 'qwen3-asr-flash-realtime', capture_reference: reference })
  expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ client_action_id: 'synthetic-input', transcript: original })
  expect(JSON.stringify(begin)).not.toContain('wav_')
  expect(fetcher.mock.calls[0][0]).toMatch(/\/voice\/realtime\/begin$/)
  expect(fetcher.mock.calls[1][0]).toMatch(/\/voice\/realtime\/complete$/)
  expect(pendingAgentRequests('synthetic-session')).toEqual([])
})

it.each([{ voice_input_id: {} }, { child_id: 'another-child' }, { client_action_id: 'another-input' },
  { source_kind: 'local_wav' }, { raw_audio_available: true }, { original_transcript: '我选B。' },
  { status: 'prepared' }])('rejects a mismatched durable capture receipt %j', async changed => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response({ status: 'prepared' }))
    .mockResolvedValueOnce(response({ ...receipt, ...changed })))
  await expect(localApi.admitRealtimeVoice('synthetic-session', 'synthetic-input', original, 100000, reference))
    .rejects.toMatchObject({ code: 'REALTIME_CAPTURE_UNCONFIRMED' })
})

it('never completes an onset on a replacement backend or after privacy abort', async () => {
  const controller = new AbortController()
  const fetcher = vi.fn().mockImplementation(async () => { controller.abort(); return response({ status: 'prepared' }) })
  vi.stubGlobal('fetch', fetcher)
  await expect(localApi.admitRealtimeVoice('synthetic-session', 'synthetic-input', original, 100000, reference, controller.signal))
    .rejects.toMatchObject({ name: 'AbortError' })
  expect(fetcher).toHaveBeenCalledTimes(1)
})

it('uses a stable Runtime action identity and does not resend an unknown paid result', async () => {
  const fetcher = vi.fn().mockRejectedValueOnce(new TypeError('synthetic lost reply'))
  vi.stubGlobal('fetch', fetcher)
  const execute = () => localApi.agentTurn('synthetic-session', original, undefined, receipt.voice_input_id,
    undefined, undefined, 'realtime-synthetic-voice')
  await expect(execute()).rejects.toMatchObject({ code: 'AGENT_REQUEST_UNCONFIRMED' })
  expect(pendingAgentRequests('synthetic-session')[0].actionId).toBe('realtime-synthetic-voice')
  await expect(execute()).rejects.toMatchObject({ code: 'AGENT_REQUEST_SAVE_FAILED' })
  expect(fetcher).toHaveBeenCalledTimes(1)
})
