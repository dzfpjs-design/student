import { afterEach, expect, it, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'
import { pendingAgentRequests } from './agentRequestJournal'

const sessionId = 'synthetic-session', childId = 'synthetic-child'
const handoffId = `voice-interaction-handoff:${'a'.repeat(64)}`
const transcript = '帮我看看这道题'
const ready = () => ({ session_id: sessionId, child_id: childId, status: 'ready', handoff_id: handoffId, agent_action_id: 'agent-a',
  deferred_progress: { childId, version: 1, step: 'deferred', updatedAt: '2026-10-15T10:20:30.123Z' },
  request: { transcript, client_action_id: 'original-a', context: { phase: 'naming' } }, receipt: null, reason: null })
const turn = () => ({ agent_turn_id: 'turn-a', session_id: sessionId, child_id: childId, transcript, action: { type: 'respond' }, tool_status: 'completed' })
const response = (body: unknown) => new Response(JSON.stringify(body))
afterEach(() => { localStorage.clear(); delete window.desktop; resetRuntimeCache(); vi.unstubAllGlobals() })

it('uses the exact scoped handoff prepare/read/invalidate contracts', async () => {
  const fetcher = vi.fn().mockImplementation(async () => response(ready()))
  vi.stubGlobal('fetch', fetcher)
  await localApi.prepareVoiceInteractionHandoff(sessionId, childId, 'original-a')
  await localApi.readVoiceInteractionHandoff(sessionId, childId)
  await localApi.invalidateVoiceInteractionHandoff(sessionId, childId)
  expect(fetcher.mock.calls.map(([url, init]) => [url.split('/').at(-1), JSON.parse(init.body)])).toEqual([
    ['prepare', { child_id: childId, original_action_key: 'original-a' }], ['read', { child_id: childId }], ['invalidate', { child_id: childId }],
  ])
})

it.each([undefined, 'original-asr'])('re-reads server proof and submits only its exact original request with optional ASR %s', async voiceInputId => {
  const proof = ready(), result = turn()
  if (voiceInputId) {
    Object.assign(proof.request, { voice_input_id: voiceInputId })
    Object.assign(result.action, { voice_input_id: voiceInputId })
  }
  const fetcher = vi.fn().mockResolvedValueOnce(response(proof)).mockResolvedValueOnce(response(result))
  vi.stubGlobal('fetch', fetcher)
  expect(await localApi.dispatchVoiceInteractionHandoff(sessionId, childId, handoffId, 'agent-a')).toEqual(result)
  expect(fetcher.mock.calls[0][0]).toMatch(/\/voice-interaction\/recovery\/handoff\/read$/)
  expect(fetcher.mock.calls[1][0]).toMatch(/\/agent-turn$/)
  expect(JSON.parse(fetcher.mock.calls[1][1].body)).toMatchObject({ transcript, client_action_id: 'agent-a', voice_interaction_handoff_id: handoffId })
  expect(JSON.parse(fetcher.mock.calls[1][1].body).voice_input_id).toBe(voiceInputId)
  expect(pendingAgentRequests(sessionId)).toEqual([])
  expect(JSON.stringify(localStorage)).not.toContain(transcript)
})

it.each(['needs_deferral', 'outcome_unknown', 'invalidated'])('guards %s without dispatching or journaling', async status => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ ...ready(), status, request: null })))
  await expect(localApi.dispatchVoiceInteractionHandoff(sessionId, childId, handoffId, 'agent-a')).rejects.toMatchObject({ code: 'VOICE_INTERACTION_HANDOFF_NOT_READY' })
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(pendingAgentRequests(sessionId)).toEqual([])
})

it('reuses an opaque pending delivery only after a fresh ready proof, without general replay', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(response(ready())).mockRejectedValueOnce(new TypeError('lost reply'))
    .mockResolvedValueOnce(response(ready())).mockResolvedValueOnce(response(turn()))
  vi.stubGlobal('fetch', fetcher)
  await expect(localApi.dispatchVoiceInteractionHandoff(sessionId, childId, handoffId, 'agent-a')).rejects.toMatchObject({ code: 'AGENT_REQUEST_UNCONFIRMED' })
  const saved = pendingAgentRequests(sessionId)[0]
  expect(saved.actionId).toBe('agent-a')
  expect(JSON.stringify(localStorage)).not.toContain(transcript)
  await expect(localApi.agentTurn(sessionId, transcript, undefined, undefined, undefined, undefined, 'agent-a')).rejects.toMatchObject({ code: 'AGENT_REQUEST_SAVE_FAILED' })
  expect(await localApi.dispatchVoiceInteractionHandoff(sessionId, childId, handoffId, 'agent-a')).toMatchObject({ agent_turn_id: 'turn-a' })
  expect(fetcher).toHaveBeenCalledTimes(4)
})

it('never dispatches after cancellation during the scoped read', async () => {
  const controller = new AbortController()
  const fetcher = vi.fn().mockImplementation(async () => { controller.abort(); return response(ready()) })
  vi.stubGlobal('fetch', fetcher)
  await expect(localApi.dispatchVoiceInteractionHandoff(sessionId, childId, handoffId, 'agent-a', controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(pendingAgentRequests(sessionId)).toEqual([])
})

it.each(['privacy', 'runtime', 'identity'])('never dispatches after a %s change during the scoped read', async change => {
  const fetcher = vi.fn().mockImplementation(async () => {
    if (change === 'privacy') window.dispatchEvent(new Event('hsc-privacy-pause'))
    if (change === 'runtime') resetRuntimeCache()
    return response({ ...ready(), ...(change === 'identity' ? { handoff_id: `voice-interaction-handoff:${'b'.repeat(64)}` } : {}) })
  })
  vi.stubGlobal('fetch', fetcher)
  await expect(localApi.dispatchVoiceInteractionHandoff(sessionId, childId, handoffId, 'agent-a')).rejects.toMatchObject({
    ...(change === 'privacy' ? { name: 'AbortError' } : { code: change === 'runtime' ? 'LOCAL_SERVICE_CHANGED' : 'VOICE_INTERACTION_HANDOFF_SCOPE_MISMATCH' }),
  })
  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(pendingAgentRequests(sessionId)).toEqual([])
})

it('accepts Runtime transcript edge trimming while preserving exact backend text on submission', async () => {
  const proof = ready()
  proof.request.transcript = ` ${transcript} `
  const fetcher = vi.fn().mockResolvedValueOnce(response(proof)).mockResolvedValueOnce(response(turn()))
  vi.stubGlobal('fetch', fetcher)
  await expect(localApi.dispatchVoiceInteractionHandoff(sessionId, childId, handoffId, 'agent-a')).resolves.toMatchObject({ agent_turn_id: 'turn-a' })
  expect(JSON.parse(fetcher.mock.calls[1][1].body).transcript).toBe(` ${transcript} `)
})

it('keeps the original opaque journal after an unknown Agent result and never retries contention', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(response(ready())).mockResolvedValueOnce(new Response(JSON.stringify({ detail: {
    code: 'CALIBRATION_WRITER_BUSY', retryable: true, recovery_action: 'retry_same_request', message: 'busy',
  } }), { status: 503 }))
  vi.stubGlobal('fetch', fetcher)
  await expect(localApi.dispatchVoiceInteractionHandoff(sessionId, childId, handoffId, 'agent-a')).rejects.toMatchObject({ code: 'CALIBRATION_WRITER_BUSY' })
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(pendingAgentRequests(sessionId)[0].actionId).toBe('agent-a')
  expect(JSON.stringify(localStorage)).not.toContain(transcript)
})

it.each([{ child_id: 'another-child' }, { session_id: 'another-session' }, { request: null }])('rejects a malformed server proof before dispatch %j', async changed => {
  const fetcher = vi.fn().mockResolvedValue(response({ ...ready(), ...changed }))
  vi.stubGlobal('fetch', fetcher)
  await expect(localApi.dispatchVoiceInteractionHandoff(sessionId, childId, handoffId, 'agent-a')).rejects.toMatchObject({ code: 'VOICE_INTERACTION_HANDOFF_SCOPE_MISMATCH' })
  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(pendingAgentRequests(sessionId)).toEqual([])
})
