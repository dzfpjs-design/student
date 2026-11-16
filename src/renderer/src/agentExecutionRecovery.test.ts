import { afterEach, expect, it, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'
import { checkAgentRequests, restoredAgentRequestMessage } from './agentRequestRecovery'
import { pendingAgentExecutions } from './agentExecutionJournal'

afterEach(() => { localStorage.clear(); resetRuntimeCache(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const turn = (status = 'pending') => ({ agent_turn_id: 'execution-one', child_id: 'child-one', session_id: 'session-one',
  tool_status: status, transcript: 'original private answer', response_text: 'private model response',
  action: { type: 'server_action', capability: 'diagnostic.answer' } })
const json = (value: unknown) => new Response(JSON.stringify(value))

it('does not forget a pending operation after a successfully delivered interpretation and restart', async () => {
  let actionId = ''
  const fetcher = vi.fn(async (_url, options) => {
    if (options.method === 'POST') { actionId = JSON.parse(options.body).client_action_id; return json(turn()) }
    return json({ session_id: 'session-one', client_action_id: actionId, status: 'interpreted',
      response_text: '理解结果已保存，但操作还没有完成回执。没有重新执行。',
      local_only: true, cloud_transmission_allowed: false, replay_allowed: false, mastery_update_allowed: false })
  })
  vi.stubGlobal('fetch', fetcher)
  await localApi.agentTurn('session-one', 'original private answer')
  resetRuntimeCache()
  expect(restoredAgentRequestMessage('session-one')).toContain('操作')
  expect(restoredAgentRequestMessage('session-two')).toBeNull()
  expect(JSON.stringify(localStorage)).not.toContain('private')
  expect(await checkAgentRequests('session-one', localApi.agentRequestReceipt, () => true)).toContain('没有完成回执')
  expect(fetcher.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1)
  expect(fetcher.mock.calls[1][0]).toContain(`/agent-request-receipts/${actionId}`)
})

it.each(['execute', 'outcome'] as const)('keeps an unknown %s result, then clears only the matching recorded terminal receipt', async endpoint => {
  const fetcher = vi.fn().mockResolvedValueOnce(json(turn())).mockRejectedValueOnce(new TypeError('lost after commit'))
  vi.stubGlobal('fetch', fetcher)
  await localApi.agentTurn('session-one', 'original private answer')
  const submit = () => endpoint === 'execute' ? localApi.executeAgentTurn('execution-one')
    : localApi.completeAgentTurn('execution-one', '操作已记录', 'completed')
  await expect(submit()).rejects.toThrow()
  resetRuntimeCache()
  expect(restoredAgentRequestMessage('session-one')).toContain('操作')
  // A read-only request check, not a second execution POST.
  const actionId = JSON.parse(fetcher.mock.calls[0][1].body).client_action_id
  fetcher.mockResolvedValueOnce(json({ session_id: 'session-one', client_action_id: actionId, status: 'recorded_completed',
    response_text: '原操作已记录完成，没有重新执行。', local_only: true, cloud_transmission_allowed: false,
    replay_allowed: false, mastery_update_allowed: false }))
  await checkAgentRequests('session-one', localApi.agentRequestReceipt, () => true)
  expect(restoredAgentRequestMessage('session-one')).toBeNull()
  expect(fetcher).toHaveBeenCalledTimes(3)
  expect(fetcher.mock.calls[2][1].method).toBeUndefined()
})

it.each(['execute', 'outcome'] as const)('settles %s on a received, matching completed outcome', async endpoint => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json(turn())).mockResolvedValueOnce(json(
    endpoint === 'execute' ? { turn: turn('completed'), execution: { capability: 'diagnostic.answer' } } : turn('completed'))))
  await localApi.agentTurn('session-one', 'answer')
  expect(restoredAgentRequestMessage('session-one')).not.toBeNull()
  if (endpoint === 'execute') await localApi.executeAgentTurn('execution-one')
  else await localApi.completeAgentTurn('execution-one', 'done', 'completed')
  expect(restoredAgentRequestMessage('session-one')).toBeNull()
})

it('does not release the action if the execution identity cannot be saved after interpretation', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw Error('full') })
    return json(turn())
  }))
  await expect(localApi.agentTurn('session-one', 'answer')).rejects.toMatchObject({ code: 'AGENT_REQUEST_UNCONFIRMED' })
  expect(restoredAgentRequestMessage('session-one')).not.toBeNull()
})

it.each([{ child_id: 'child-two' }, { session_id: 'session-two' }, { agent_turn_id: 'another-turn' }, { tool_status: 'pending' }])(
  'does not settle from a mismatched or nonterminal execution (%j)', async patch => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json(turn())).mockResolvedValueOnce(json({ turn: { ...turn('completed'), ...patch } })))
    await localApi.agentTurn('session-one', 'answer')
    await localApi.executeAgentTurn('execution-one')
    expect(pendingAgentExecutions('session-one')).toHaveLength(1)
  })

it.each(['runtime', 'privacy'] as const)('does not settle a late result after %s changes', async change => {
  let release!: (value: Response) => void
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json(turn())).mockImplementationOnce(() => new Promise(resolve => { release = resolve })))
  await localApi.agentTurn('session-one', 'answer')
  const result = localApi.executeAgentTurn('execution-one').catch(error => error)
  await vi.waitFor(() => expect(release).toBeTypeOf('function'))
  if (change === 'runtime') resetRuntimeCache()
  else window.dispatchEvent(new Event('hsc-privacy-pause'))
  release(json({ turn: turn('completed') }))
  expect(await result).toMatchObject(change === 'runtime' ? { code: 'LOCAL_SERVICE_CHANGED' } : { name: 'AbortError' })
  expect(pendingAgentExecutions('session-one')).toHaveLength(1)
})

it('does not create a new execution journal from a late interpretation after privacy pause', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => { window.dispatchEvent(new Event('hsc-privacy-pause')); return json(turn()) }))
  await expect(localApi.agentTurn('session-one', 'answer')).rejects.toMatchObject({ name: 'AbortError' })
  expect(pendingAgentExecutions('session-one')).toEqual([])
  expect(restoredAgentRequestMessage('session-one')).not.toBeNull()
})

it('does not retire a newer identity at the same turn while an old outcome is in flight', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json(turn())).mockImplementationOnce(async () => {
    const key = Object.keys(localStorage).find(k => k.startsWith('hsc-agent-execution-v1:'))!
    const value = JSON.parse(localStorage.getItem(key)!)
    localStorage.setItem(key, JSON.stringify({ ...value, actionId: 'new-action' }))
    return json({ turn: turn('completed') })
  }))
  await localApi.agentTurn('session-one', 'answer')
  await localApi.executeAgentTurn('execution-one')
  expect(pendingAgentExecutions('session-one')[0].actionId).toBe('new-action')
})

it('leaves no execution journal for already completed conversational turns', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json(turn('completed'))))
  await localApi.agentTurn('session-one', 'hello')
  expect(restoredAgentRequestMessage('session-one')).toBeNull()
})

it('deduplicates delivery plus execution after the delivery cleanup write fails', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json(turn())))
  const remove = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw Error('locked') })
  await localApi.agentTurn('session-one', 'answer')
  remove.mockRestore()
  const actionId = pendingAgentExecutions('session-one')[0].actionId
  const read = vi.fn(async () => ({ session_id: 'session-one', client_action_id: actionId, status: 'interpreted' as const,
    response_text: '操作还没有完成回执。', local_only: true as const, cloud_transmission_allowed: false as const,
    replay_allowed: false as const, mastery_update_allowed: false as const }))
  await checkAgentRequests('session-one', read, () => true)
  expect(read).toHaveBeenCalledTimes(1)
  expect(restoredAgentRequestMessage('session-one')).toContain('操作')
  expect(pendingAgentExecutions('session-one')).toHaveLength(1)
})
