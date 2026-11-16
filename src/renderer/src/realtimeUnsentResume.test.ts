import { afterEach, expect, it, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'
import { pendingAgentRequests, saveAgentRequest } from './agentRequestJournal'
import { pendingAgentExecutions, saveAgentExecution } from './agentExecutionJournal'
import { pendingRealtimeAdmissions, prepareRealtimeAdmission, updateRealtimeAdmission } from './realtimeAdmissionJournal'
import { isRealtimeResumeRequest, shouldResumeRealtimeInput, resumeOneUnsentInput, settleUnsentResumeJournal, type RealtimeResumePreview } from './realtimeUnsentResume'
import type { AgentTurn } from './types'

afterEach(() => { localStorage.clear(); resetRuntimeCache(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers() })
const scope = { sessionId: 'synthetic-session', childId: 'synthetic-child', inputId: 'synthetic-original' }
const voice = 'synthetic-canonical-voice'
const seed = () => updateRealtimeAdmission(prepareRealtimeAdmission(scope.sessionId, scope.childId, scope.inputId), 'stored', voice)
const preview = (status: RealtimeResumePreview['status'] = 'ready'): RealtimeResumePreview => ({
  session_id: scope.sessionId, child_id: scope.childId, input_id: scope.inputId, voice_input_id: voice,
  status, resume_allowed: status === 'ready', response_text: '合成原话的本机核对', local_only: true,
  cloud_transmission_allowed: false, replay_allowed: false, mastery_update_allowed: false, raw_audio_available: false,
})
const turn = (): AgentTurn => ({ session_id: scope.sessionId, child_id: scope.childId,
  agent_turn_id: 'original-runtime-turn', transcript: '孩子之前保存的完整原话', intent: 'capture', response_text: '看看原来的题目',
  action: { type: 'request_capture', voice_input_id: voice }, tool_status: 'pending' } as AgentTurn)

it.each(['继续刚才的话', '请继续刚才未处理的原话', '帮我恢复处理上次保存的语音'])('recognizes explicit recovery %s without treating ordinary continuation as recovery', text => {
  expect(isRealtimeResumeRequest(text)).toBe(true)
  expect(isRealtimeResumeRequest('继续讲下一道题')).toBe(false)
})

it('does not steal normal conversational continuation when no unsent recovery record exists', () => {
  expect(shouldResumeRealtimeInput('继续刚才的话', scope.sessionId, scope.childId)).toBe(false)
  expect(shouldResumeRealtimeInput('继续刚才没发出的原话', scope.sessionId, scope.childId)).toBe(true)
  seed()
  expect(shouldResumeRealtimeInput('继续刚才的话', scope.sessionId, scope.childId)).toBe(true)
})

it('does not throw or erase recovery when completion cleanup storage is unavailable or cancellation has changed ownership', () => {
  const saved = updateRealtimeAdmission(seed(), 'dispatching')
  const broken = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw Error('synthetic storage unavailable') })
  expect(settleUnsentResumeJournal(saved)).toBe(false)
  broken.mockRestore()
  expect(pendingRealtimeAdmissions(scope.sessionId, scope.childId)).toEqual([saved])
  const cancelled = updateRealtimeAdmission(saved, 'cancel_requested')
  expect(settleUnsentResumeJournal(saved)).toBe(false)
  expect(pendingRealtimeAdmissions(scope.sessionId, scope.childId)).toEqual([cancelled])
})

it('resumes the original identity only and preserves pending execution after a lost response', async () => {
  seed()
  const action = 'realtime-' + voice
  const saved = saveAgentRequest(scope.sessionId, action)
  const fetcher = vi.fn().mockRejectedValueOnce(new TypeError('lost after claim'))
    .mockResolvedValueOnce(new Response(JSON.stringify(turn())))
  vi.stubGlobal('fetch', fetcher)
  await expect(localApi.resumeRealtimeVoice(scope.sessionId, scope.childId, scope.inputId, voice))
    .rejects.toMatchObject({ code: 'AGENT_REQUEST_UNCONFIRMED' })
  expect(pendingAgentRequests(scope.sessionId)).toEqual([saved])
  await localApi.resumeRealtimeVoice(scope.sessionId, scope.childId, scope.inputId, voice)
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ child_id: scope.childId, voice_input_id: voice })
  expect(JSON.stringify(localStorage)).not.toContain('孩子之前')
  expect(pendingAgentRequests(scope.sessionId)).toEqual([])
  expect(pendingAgentExecutions(scope.sessionId)).toMatchObject([{ actionId: action, turnId: 'original-runtime-turn' }])
})

it.each([1, 128])('preserves an existing execution journal timestamp even at capacity %s', async capacity => {
  seed()
  const request = { version: 1 as const, sessionId: scope.sessionId, actionId: 'realtime-' + voice,
    createdAt: '2026-10-01T00:00:00.000Z' }
  saveAgentExecution(request, scope.childId, 'original-runtime-turn')
  for (let i = 1; i < capacity; i++) saveAgentExecution({ ...request, actionId: 'synthetic-other-' + i }, scope.childId, 'other-turn-' + i)
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(turn()))))
  await localApi.resumeRealtimeVoice(scope.sessionId, scope.childId, scope.inputId, voice)
  expect(pendingAgentRequests(scope.sessionId)).toEqual([])
  expect(pendingAgentExecutions(scope.sessionId)).toContainEqual({ ...request, childId: scope.childId, turnId: 'original-runtime-turn' })
  expect(pendingAgentExecutions(scope.sessionId)).toHaveLength(capacity)
})

it.each(['foreign-child', 'changed-turn'] as const)('never replaces an existing %s execution identity', async change => {
  seed()
  const request = { version: 1 as const, sessionId: scope.sessionId, actionId: 'realtime-' + voice,
    createdAt: '2026-10-01T00:00:00.000Z' }
  saveAgentExecution(request, change === 'foreign-child' ? 'different-child' : scope.childId, 'original-runtime-turn')
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ...turn(), agent_turn_id: 'different-turn' })))
  vi.stubGlobal('fetch', fetcher)
  await expect(localApi.resumeRealtimeVoice(scope.sessionId, scope.childId, scope.inputId, voice))
    .rejects.toMatchObject({ code: 'AGENT_REQUEST_UNCONFIRMED' })
  expect(fetcher).toHaveBeenCalledTimes(change === 'foreign-child' ? 0 : 1)
  expect(pendingAgentExecutions(scope.sessionId)).toHaveLength(1)
  expect(pendingAgentExecutions(scope.sessionId)[0].turnId).toBe('original-runtime-turn')
})

it.each(['blocked', 'tampered', 'cancelled'] as const)('never dispatches a %s original even if browser phase says stored', async reason => {
  const item = seed(), dispatch = vi.fn()
  const read = vi.fn(async () => {
    if (reason === 'cancelled') updateRealtimeAdmission(item, 'cancel_requested')
    return { ...preview(reason === 'blocked' ? 'blocked' : 'ready'), ...(reason === 'tampered' ? { child_id: 'other-child' } : {}) }
  })
  const result = resumeOneUnsentInput(scope.sessionId, scope.childId, read, dispatch, () => true, new AbortController().signal)
  if (reason === 'tampered') await expect(result).rejects.toThrow('scope mismatch')
  else await result
  expect(dispatch).not.toHaveBeenCalled()
  expect(pendingRealtimeAdmissions(scope.sessionId, scope.childId)).toHaveLength(1)
})

it('dispatches saved source rather than recovery words and retains its recovery journal until execution is known', async () => {
  seed()
  const dispatch = vi.fn().mockResolvedValue(turn())
  const result = await resumeOneUnsentInput(scope.sessionId, scope.childId, async () => preview(), dispatch, () => true, new AbortController().signal)
  expect(dispatch).toHaveBeenCalledExactlyOnceWith(scope.sessionId, scope.childId, scope.inputId, voice, expect.any(AbortSignal))
  expect(result.turn?.transcript).toBe('孩子之前保存的完整原话')
  expect(pendingRealtimeAdmissions(scope.sessionId, scope.childId)).toMatchObject([{ phase: 'dispatching' }])
})

it.each(['valid', 'changed-turn', 'device', 'evaluation', 'completed'] as const)('resumes only the original unstarted server turn: %s', async variant => {
  seed()
  const original = { ...turn(), runtime_execution_location: 'server', runtime_capability: 'system.inspect_learning' } as AgentTurn
  const saved = { ...preview(), resume_kind: 'execution' as const, agent_turn_id: original.agent_turn_id }
  if (variant === 'changed-turn') original.agent_turn_id = 'different-runtime-turn'
  if (variant === 'device') original.runtime_execution_location = 'client_device_bridge'
  if (variant === 'evaluation') original.runtime_capability = 'learning.evaluate_response'
  if (variant === 'completed') original.tool_status = 'completed'
  const result = resumeOneUnsentInput(scope.sessionId, scope.childId, async () => saved,
    vi.fn().mockResolvedValue(original), () => true, new AbortController().signal)
  if (variant === 'valid') expect((await result).turn).toEqual(original)
  else await expect(result).rejects.toThrow('scope mismatch')
  expect(pendingRealtimeAdmissions(scope.sessionId, scope.childId)).toHaveLength(1)
})

it('does not dispatch when planned preview omits original turn identity', async () => {
  seed()
  const dispatch = vi.fn()
  await expect(resumeOneUnsentInput(scope.sessionId, scope.childId,
    async () => ({ ...preview(), resume_kind: 'execution' }), dispatch, () => true, new AbortController().signal))
    .rejects.toThrow('scope mismatch')
  expect(dispatch).not.toHaveBeenCalled()
})

it.each(['valid', 'different-evaluation', 'grading', 'missing-preview'] as const)('keeps the original restricted evaluation scope on resume: %s', async variant => {
  seed()
  const stored = { ...preview(), pending_evaluation_turn_id: variant === 'missing-preview' ? undefined : 'original-evaluation' }
  const old = { ...turn(), runtime_capability: 'conversation.respond', runtime_execution_location: 'server', after_execution: 'stop',
    tool_status: 'completed', action: { type: 'none', voice_input_id: voice,
      pending_evaluation_control: { version: 'pending_evaluation_control_v1', mode: 'control_only',
        evaluation_turn_id: variant === 'different-evaluation' ? 'different-evaluation' : 'original-evaluation' } } } as AgentTurn
  if (variant === 'grading') old.runtime_capability = 'learning.evaluate_response'
  const result = resumeOneUnsentInput(scope.sessionId, scope.childId, async () => stored, async () => old,
    () => true, new AbortController().signal)
  if (variant === 'valid') expect((await result).pendingEvaluationTurnId).toBe('original-evaluation')
  else await expect(result).rejects.toThrow('evaluation scope mismatch')
  expect(pendingRealtimeAdmissions(scope.sessionId, scope.childId)).toHaveLength(1)
})

it('does not leapfrog an unknown older input or accept late preview after owner changes', async () => {
  seed()
  await new Promise(resolve => setTimeout(resolve, 5))
  updateRealtimeAdmission(prepareRealtimeAdmission(scope.sessionId, scope.childId, 'synthetic-later-input'), 'stored', 'synthetic-later-voice')
  const dispatch = vi.fn(), read = vi.fn().mockResolvedValue(preview('blocked'))
  await resumeOneUnsentInput(scope.sessionId, scope.childId, read, dispatch, () => true, new AbortController().signal)
  expect(read).toHaveBeenCalledTimes(1)
  expect(dispatch).not.toHaveBeenCalled()
  let current = true
  await resumeOneUnsentInput(scope.sessionId, scope.childId, async () => { current = false; return preview() }, dispatch, () => current, new AbortController().signal)
  expect(dispatch).not.toHaveBeenCalled()
})

it('releases a hung readonly preview without sending the original or clearing its identity', async () => {
  vi.useFakeTimers()
  const saved = seed(), dispatch = vi.fn()
  const work = resumeOneUnsentInput(scope.sessionId, scope.childId, () => new Promise(() => {}), dispatch, () => true, new AbortController().signal)
  const rejected = expect(work).rejects.toMatchObject({ code: 'VOICE_RECEIPT_CHECK_TIMEOUT' })
  await vi.advanceTimersByTimeAsync(10_001)
  await rejected
  expect(dispatch).not.toHaveBeenCalled()
  expect(pendingRealtimeAdmissions(scope.sessionId, scope.childId)).toEqual([saved])
})

it('bounds an ignored dispatch abort and preserves unknown delivery rather than retrying', async () => {
  vi.useFakeTimers()
  seed()
  let dispatchSignal: AbortSignal | undefined
  const dispatch = vi.fn((_s, _c, _i, _v, signal?: AbortSignal) => { dispatchSignal = signal; return new Promise<AgentTurn>(() => {}) })
  const work = resumeOneUnsentInput(scope.sessionId, scope.childId, async () => preview(), dispatch, () => true, new AbortController().signal)
  const rejected = expect(work).rejects.toMatchObject({ code: 'REALTIME_RESUME_UNCONFIRMED' })
  await vi.advanceTimersByTimeAsync(180_001)
  await rejected
  expect(dispatch).toHaveBeenCalledTimes(1)
  expect(dispatchSignal?.aborted).toBe(true)
  expect(pendingRealtimeAdmissions(scope.sessionId, scope.childId)).toMatchObject([{ phase: 'dispatching' }])
})

it.each(['wrong_scope', 'privacy', 'aborted', 'conflict'] as const)('retains canonical delivery and never consumes a %s resume response', async reason => {
  seed()
  const abort = new AbortController()
  const fetcher = vi.fn(async () => {
    if (reason === 'privacy') window.dispatchEvent(new CustomEvent('hsc-privacy-pause'))
    if (reason === 'aborted') abort.abort()
    if (reason === 'conflict') return new Response(JSON.stringify({ code: 'REALTIME_RESUME_NOT_UNSENT', message: '仍需核对。' }), { status: 409 })
    return new Response(JSON.stringify({ ...turn(), ...(reason === 'wrong_scope' ? { child_id: 'different-child' } : {}) }))
  })
  vi.stubGlobal('fetch', fetcher)
  await expect(localApi.resumeRealtimeVoice(scope.sessionId, scope.childId, scope.inputId, voice, abort.signal)).rejects.toThrow()
  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(pendingAgentRequests(scope.sessionId)).toHaveLength(1)
  expect(pendingAgentExecutions(scope.sessionId)).toEqual([])
  expect(pendingRealtimeAdmissions(scope.sessionId, scope.childId)).toHaveLength(1)
})
