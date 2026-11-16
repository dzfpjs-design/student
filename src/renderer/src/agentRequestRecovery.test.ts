import { afterEach, expect, it, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'
import { checkAgentRequests, restoredAgentRequestMessage, type AgentRequestReceipt } from './agentRequestRecovery'
import { pendingAgentRequests, saveAgentRequest } from './agentRequestJournal'
import { pendingAgentExecutions, saveAgentExecution } from './agentExecutionJournal'

afterEach(() => { localStorage.clear(); resetRuntimeCache(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers() })
const receipt = (session: string, action: string, status: AgentRequestReceipt['status'] = 'interpreted'): AgentRequestReceipt => ({
  session_id: session, client_action_id: action, status, response_text: '原请求已保存，后续操作尚未确认。',
  local_only: true, cloud_transmission_allowed: false, replay_allowed: false, mastery_update_allowed: false,
})

it('journals before submission and recovers a lost reply by read only after runtime reset', async () => {
  let original = ''
  const fetcher = vi.fn().mockImplementationOnce(async (_url, options) => {
    original = JSON.parse(options.body).client_action_id
    expect(pendingAgentRequests('session-one')[0].actionId).toBe(original)
    throw new TypeError('response lost after commit')
  }).mockImplementationOnce(async () => new Response(JSON.stringify(receipt('session-one', original))))
  vi.stubGlobal('fetch', fetcher)
  await expect(localApi.agentTurn('session-one', '这是孩子的完整答案')).rejects.toMatchObject({ code: 'AGENT_REQUEST_UNCONFIRMED' })
  expect(JSON.stringify(localStorage)).not.toContain('这是孩子的完整答案')
  resetRuntimeCache()
  expect(restoredAgentRequestMessage('session-one')).toContain('不会自动重做')
  expect(await checkAgentRequests('session-one', localApi.agentRequestReceipt, () => true)).toContain('尚未确认')
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(fetcher.mock.calls[1][0]).toContain(`/agent-request-receipts/${original}`)
  expect(fetcher.mock.calls[1][1].method).toBeUndefined()
  expect(pendingAgentRequests('session-one')).toHaveLength(1)
})

it('does not submit before a durable recovery identity can be saved', async () => {
  const fetcher = vi.fn()
  vi.stubGlobal('fetch', fetcher)
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('disk full') })
  await expect(localApi.agentTurn('session-one', '继续')).rejects.toMatchObject({ code: 'AGENT_REQUEST_SAVE_FAILED' })
  expect(fetcher).not.toHaveBeenCalled()
})

it.each(['interpreted', 'received', 'unconfirmed'] as const)('retains a %s request without treating absence as permission to replay', async status => {
  const pending = saveAgentRequest('session-one', 'operation-1')
  await checkAgentRequests('session-one', async () => receipt('session-one', 'operation-1', status), () => true)
  expect(pendingAgentRequests('session-one')).toEqual([pending])
})

it('keeps interpreted delivery and execution identities until an authoritative terminal receipt', async () => {
  const pending = saveAgentRequest('session-interpreted', 'original-interpreted')
  saveAgentExecution(pending, 'child-interpreted', 'turn-interpreted')
  const read = vi.fn(async () => receipt(pending.sessionId, pending.actionId, 'interpreted'))
  for (let index = 0; index < 2; index++) {
    resetRuntimeCache()
    await checkAgentRequests(pending.sessionId, read, () => true)
    expect(pendingAgentRequests(pending.sessionId)).toEqual([pending])
    expect(pendingAgentExecutions(pending.sessionId)).toHaveLength(1)
  }
  await checkAgentRequests(pending.sessionId, async () => receipt(pending.sessionId, pending.actionId, 'recorded_completed'), () => true)
  expect(pendingAgentRequests(pending.sessionId)).toEqual([])
  expect(pendingAgentExecutions(pending.sessionId)).toEqual([])
})

it.each(['AGENT_HANDOFF_UNCONFIRMED', 'AGENT_HANDOFF_SOURCE_CONFLICT'])('preserves a %s handoff identity after HTTP rejection and readonly recovery', async code => {
  let action = ''
  const fetcher = vi.fn().mockImplementationOnce(async (_url, options) => {
    action = JSON.parse(options.body).client_action_id
    return new Response(JSON.stringify({ code, message: '原交接尚未确认，不会自动重做。' }), { status: 409 })
  }).mockImplementationOnce(async () => new Response(JSON.stringify(receipt('session-handoff', action, 'unconfirmed'))))
  vi.stubGlobal('fetch', fetcher)
  await expect(localApi.agentTurn('session-handoff', '合成原作答')).rejects.toMatchObject({ status: 409, code })
  const pending = pendingAgentRequests('session-handoff')
  expect(pending).toHaveLength(1)
  resetRuntimeCache()
  await checkAgentRequests('session-handoff', localApi.agentRequestReceipt, () => true)
  expect(pendingAgentRequests('session-handoff')).toEqual(pending)
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(fetcher.mock.calls[1][1].method).toBeUndefined()
  expect(JSON.stringify(localStorage)).not.toContain('合成原作答')
})

it('ignores a late receipt after switching session or stopping listening', async () => {
  const pending = saveAgentRequest('session-one', 'operation-1')
  let current = true
  const text = await checkAgentRequests('session-one', async () => { current = false; return receipt('session-one', 'operation-1') }, () => current)
  expect(text).toBeNull()
  expect(pendingAgentRequests('session-one')).toEqual([pending])
})

it.each([{ session_id: 'session-other' }, { client_action_id: 'wrong' }, { replay_allowed: true }, { cloud_transmission_allowed: true }])('rejects mismatched or replayable receipts (%j)', async change => {
  saveAgentRequest('session-one', 'operation-1')
  await expect(checkAgentRequests('session-one', async () => ({ ...receipt('session-one', 'operation-1'), ...change }) as AgentRequestReceipt, () => true)).rejects.toThrow()
  expect(pendingAgentRequests('session-one')).toHaveLength(1)
})

it('acknowledges only delivery on a normal successful reply', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"agent_turn_id":"saved"}')))
  await expect(localApi.agentTurn('session-one', '继续')).resolves.toEqual({ agent_turn_id: 'saved' })
  expect(pendingAgentRequests('session-one')).toEqual([])
})

it.each(['{}', 'null', '{broken'])('retains an unreadable successful HTTP body (%s)', async body => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body)))
  await expect(localApi.agentTurn('session-one', '继续')).rejects.toMatchObject({ code: 'AGENT_REQUEST_UNCONFIRMED' })
  expect(pendingAgentRequests('session-one')).toHaveLength(1)
})

it('does not let eight unresolved requests starve later delivery checks', async () => {
  for (let i = 0; i < 12; i++) saveAgentRequest('session-backlog', `operation-${String(i).padStart(2, '0')}`)
  const read = vi.fn(async (session: string, action: string) => receipt(session, action, 'unconfirmed'))
  await checkAgentRequests('session-backlog', read, () => true)
  await checkAgentRequests('session-backlog', read, () => true)
  expect(new Set(read.mock.calls.map(([, action]) => action)).size).toBe(12)
  expect(pendingAgentRequests('session-backlog')).toHaveLength(12)
})

it('releases a hung receipt after ten seconds and never acknowledges its late completed response', async () => {
  vi.useFakeTimers()
  const pending = saveAgentRequest('session-timeout', 'timeout-original')
  let finish!: (value: AgentRequestReceipt) => void
  const read = vi.fn((_session, _key, _signal?: AbortSignal) => new Promise<AgentRequestReceipt>(resolve => { finish = resolve }))
  const checked = checkAgentRequests('session-timeout', read, () => true)
  const rejected = expect(checked).rejects.toMatchObject({ code: 'VOICE_RECEIPT_CHECK_TIMEOUT' })
  await vi.advanceTimersByTimeAsync(10_000)
  await rejected
  expect(read.mock.calls[0][2]?.aborted).toBe(true)
  finish(receipt('session-timeout', 'timeout-original', 'recorded_completed'))
  await vi.advanceTimersByTimeAsync(0)
  expect(pendingAgentRequests('session-timeout')).toEqual([pending])
  expect(vi.getTimerCount()).toBe(0)
})

it('advances past a failed lookup on the next explicit check without retrying the failed request', async () => {
  for (let index = 0; index < 3; index++) saveAgentRequest('session-failure', `failed-lookup-${index}`)
  const broken = vi.fn().mockRejectedValue(new Error('offline'))
  await expect(checkAgentRequests('session-failure', broken, () => true)).rejects.toThrow('offline')
  const first = broken.mock.calls[0][1]
  const read = vi.fn(async (session, key) => receipt(session, key, 'unconfirmed'))
  await checkAgentRequests('session-failure', read, () => true)
  expect(read.mock.calls[0][1]).not.toBe(first)
  expect(pendingAgentRequests('session-failure')).toHaveLength(3)
})

it('passes a cancellable no-store signal to actual receipt HTTP and sends nothing after delayed address cancellation', async () => {
  const controller = new AbortController()
  let ready!: (value: unknown) => void
  vi.stubGlobal('desktop', { getRuntimeConfig: () => new Promise(resolve => { ready = resolve }) })
  const fetcher = vi.fn()
  vi.stubGlobal('fetch', fetcher)
  const result = localApi.agentRequestReceipt('session-delay', 'original', controller.signal).catch(error => error)
  controller.abort()
  ready({ status: 'ready', localApiUrl: 'http://127.0.0.1:9876', sessionToken: 'synthetic-only' })
  expect(await result).toMatchObject({ name: 'AbortError' })
  expect(fetcher).not.toHaveBeenCalled()
  const active = new AbortController()
  fetcher.mockResolvedValueOnce(new Response(JSON.stringify(receipt('session-delay', 'original'))))
  await localApi.agentRequestReceipt('session-delay', 'original', active.signal)
  expect(fetcher.mock.calls[0][1]).toMatchObject({ cache: 'no-store', signal: active.signal })
})

it('rejects a receipt whose body finishes only after its owner cancels', async () => {
  const parent = new AbortController()
  let finish!: (value: AgentRequestReceipt) => void
  const response = { ok: true, json: () => new Promise<AgentRequestReceipt>(resolve => { finish = resolve }) }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response))
  const result = localApi.agentRequestReceipt('session-body', 'original-body', parent.signal)
  const rejected = expect(result).rejects.toMatchObject({ name: 'AbortError' })
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
  parent.abort()
  finish(receipt('session-body', 'original-body', 'recorded_completed'))
  await rejected
})

it.each([401, 403, 409, 422])('does not mistake an explicit HTTP rejection (%s) for a lost reply', async status => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: 'EXPLICIT_REJECTION', message: '请求未通过当前校验。' }), { status })))
  await expect(localApi.agentTurn('session-rejected', '继续')).rejects.toMatchObject({ status, code: 'EXPLICIT_REJECTION' })
  expect(pendingAgentRequests('session-rejected')).toEqual([])
})

it('preserves an unknown result when HTTP headers arrive but reading the reply stream fails', async () => {
  const body = new ReadableStream({ start(controller) { controller.error(new TypeError('reply stream disconnected')) } })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body)))
  await expect(localApi.agentTurn('session-stream', '十二')).rejects.toMatchObject({ code: 'AGENT_REQUEST_UNCONFIRMED' })
  expect(pendingAgentRequests('session-stream')).toHaveLength(1)
})

it.each(['privacy', 'runtime'] as const)('does not send after %s changes while resolving the initial service address', async reason => {
  let ready!: (value: unknown) => void
  const lookup = new Promise(resolve => { ready = resolve })
  vi.stubGlobal('desktop', { getRuntimeConfig: vi.fn(() => lookup) })
  const fetcher = vi.fn().mockResolvedValue(new Response('{"agent_turn_id":"late-post"}'))
  vi.stubGlobal('fetch', fetcher)
  const result = localApi.agentTurn('session-one', 'not yet submitted').catch(error => error)
  expect(fetcher).not.toHaveBeenCalled()
  if (reason === 'privacy') window.dispatchEvent(new Event('hsc-privacy-pause'))
  else resetRuntimeCache()
  ready({ status: 'ready', localApiUrl: 'http://127.0.0.1:9876', sessionToken: 'test-only' })
  const error = await result
  expect(fetcher).not.toHaveBeenCalled()
  expect(error).toMatchObject(reason === 'privacy' ? { name: 'AbortError' } : { code: 'LOCAL_SERVICE_CHANGED' })
  expect(pendingAgentRequests('session-one')).toHaveLength(1)
  if (reason === 'runtime') {
    const newConfig = vi.fn(async () => ({ status: 'ready', localApiUrl: 'http://127.0.0.1:9877', sessionToken: 'new-only' }))
    vi.stubGlobal('desktop', { getRuntimeConfig: newConfig })
    await localApi.agentTurn('session-two', 'fresh request')
    expect(newConfig).toHaveBeenCalledTimes(1)
    expect(fetcher.mock.calls[0][0]).toBe('http://127.0.0.1:9877/v1/sessions/session-two/agent-turn')
    expect(fetcher.mock.calls[0][1].headers['X-HSC-Session']).toBe('new-only')
  }
})
