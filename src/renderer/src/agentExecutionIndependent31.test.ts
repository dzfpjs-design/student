import { afterEach, expect, it, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'
import { pendingAgentRequests, saveAgentRequest } from './agentRequestJournal'
import { agentExecutionSnapshot, pendingAgentExecutions, saveAgentExecution, settleAgentExecution } from './agentExecutionJournal'
import { checkAgentRequests } from './agentRequestRecovery'

afterEach(() => {
  localStorage.clear(); resetRuntimeCache(); vi.restoreAllMocks(); vi.unstubAllGlobals()
  delete window.desktop
})

function identity(session = 'session-one', child = 'child-one', action = 'action-one', turn = 'turn-one') {
  const request = saveAgentRequest(session, action)
  saveAgentExecution(request, child, turn)
  return request
}

function receipt(session_id: string, client_action_id: string, status = 'recorded_completed') {
  return { session_id, client_action_id, status, response_text: 'Only a local receipt; no execution.',
    local_only: true, cloud_transmission_allowed: false, replay_allowed: false, mastery_update_allowed: false } as const
}

it('a scoped terminal snapshot cannot retire another child even with the same turn identifier', () => {
  identity()
  identity('session-two', 'child-two', 'action-two')
  const snapshot = agentExecutionSnapshot('turn-one')
  expect(snapshot).toHaveLength(2)
  settleAgentExecution(snapshot, { agent_turn_id: 'turn-one', child_id: 'child-one', session_id: 'session-one', tool_status: 'completed' })
  expect(pendingAgentExecutions('session-one')).toHaveLength(0)
  expect(pendingAgentExecutions('session-two')).toHaveLength(1)
})

it('same action in another session is neither read nor acknowledged', async () => {
  identity()
  identity('session-two', 'child-two', 'action-one', 'turn-two')
  const reader = vi.fn(async (session, action) => receipt(session, action) as any)
  await checkAgentRequests('session-one', reader, () => true)
  expect(reader.mock.calls).toEqual([['session-one', 'action-one', expect.any(AbortSignal)]])
  expect(pendingAgentRequests('session-two')).toHaveLength(1)
  expect(pendingAgentExecutions('session-two')).toHaveLength(1)
})

it('a matching old read receipt cannot delete replacement delivery or execution metadata', async () => {
  identity()
  const reader = vi.fn(async (session, action) => {
    for (const key of Object.keys(localStorage)) {
      const saved = JSON.parse(localStorage.getItem(key)!)
      localStorage.setItem(key, JSON.stringify({ ...saved, createdAt: '2027-01-01T00:00:00Z' }))
    }
    return receipt(session, action) as any
  })
  await checkAgentRequests('session-one', reader, () => true)
  expect(pendingAgentRequests('session-one')).toHaveLength(1)
  expect(pendingAgentExecutions('session-one')).toHaveLength(1)
})

it('mixing delivery and execution unknowns does not starve a later execution-only identity', async () => {
  for (let i = 0; i < 9; i++) identity('session-one', 'child-one', `action-${i}`, `turn-${i}`)
  const reader = vi.fn(async (session, action) => receipt(session, action, 'unconfirmed') as any)
  await checkAgentRequests('session-one', reader, () => true)
  expect(new Set(reader.mock.calls.map(([, action]) => action)).size).toBe(8)
  await checkAgentRequests('session-one', reader, () => true)
  expect(new Set(reader.mock.calls.map(([, action]) => action)).size).toBe(9)
  expect(pendingAgentExecutions('session-one')).toHaveLength(9)
})

it.each(['execute', 'outcome'] as const)('unreadable owned storage prevents %s dispatch', async endpoint => {
  identity()
  const fetcher = vi.fn()
  vi.stubGlobal('fetch', fetcher)
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw Error('unavailable') })
  const operation = endpoint === 'execute' ? localApi.executeAgentTurn('turn-one')
    : localApi.completeAgentTurn('turn-one', 'done', 'completed')
  await expect(operation).rejects.toThrow('unavailable')
  expect(fetcher).not.toHaveBeenCalled()
})

it.each(['execute', 'outcome'] as const)('privacy pause before runtime discovery prevents %s POST/PUT', async endpoint => {
  identity()
  let release!: (value: unknown) => void
  window.desktop = { getRuntimeConfig: vi.fn(() => new Promise(resolve => { release = resolve })) } as any
  const fetcher = vi.fn()
  vi.stubGlobal('fetch', fetcher)
  const operation = (endpoint === 'execute' ? localApi.executeAgentTurn('turn-one')
    : localApi.completeAgentTurn('turn-one', 'done', 'completed')).catch(error => error)
  window.dispatchEvent(new Event('hsc-privacy-pause'))
  release({ status: 'ready', localApiUrl: 'http://127.0.0.1:9999', sessionToken: 'isolated' })
  expect(await operation).toMatchObject({ name: 'AbortError' })
  expect(fetcher).not.toHaveBeenCalled()
  expect(pendingAgentExecutions('session-one')).toHaveLength(1)
})

it.each(['replay_allowed', 'mastery_update_allowed'] as const)('a read receipt asserting %s is not trusted to retire an execution', async field => {
  identity()
  const read = vi.fn(async (session, action) => ({ ...receipt(session, action), [field]: true }) as any)
  await expect(checkAgentRequests('session-one', read, () => true)).rejects.toThrow('scope mismatch')
  expect(pendingAgentExecutions('session-one')).toHaveLength(1)
})
