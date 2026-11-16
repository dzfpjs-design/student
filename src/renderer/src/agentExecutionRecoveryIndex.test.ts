import { afterEach, expect, it } from 'vitest'
import { restoreAgentExecutionIdentities, pendingAgentExecutions, saveAgentExecution } from './agentExecutionJournal'

const index = () => ({ child_id: 'child-a', session_id: 'session-a', local_only: true as const,
  cloud_transmission_allowed: false as const, replay_allowed: false as const, has_more: false,
  entries: [{ agent_turn_id: 'original-turn', client_action_id: 'original-action', created_at: '2026-10-07T13:58:13.421Z' }] })
afterEach(() => localStorage.clear())

it('recovers a crash-lost original identity without private content or execution and preserves existing metadata', () => {
  expect(restoreAgentExecutionIdentities('session-a', 'child-a', index())).toBe(1)
  expect(pendingAgentExecutions('session-a')).toEqual([{ version: 1, childId: 'child-a', sessionId: 'session-a',
    turnId: 'original-turn', actionId: 'original-action', createdAt: '2026-10-07T13:58:13.421Z' }])
  expect(restoreAgentExecutionIdentities('session-a', 'child-a', index())).toBe(0)
})

it.each(['owner', 'privacy', 'extra', 'duplicate', 'conflict'] as const)('rejects a %s index before adding any identity', reason => {
  const payload = index()
  if (reason === 'owner') payload.child_id = 'other-child'
  if (reason === 'privacy') Object.assign(payload, { cloud_transmission_allowed: true })
  if (reason === 'extra') Object.assign(payload.entries[0], { answer: 'private' })
  if (reason === 'duplicate') payload.entries.push({ ...payload.entries[0] })
  if (reason === 'conflict') saveAgentExecution({ version: 1, sessionId: 'session-a', actionId: 'other-action',
    createdAt: '2026-10-07T13:58:13.421Z' }, 'child-a', 'original-turn')
  const before = pendingAgentExecutions('session-a')
  expect(() => restoreAgentExecutionIdentities('session-a', 'child-a', payload)).toThrow()
  expect(pendingAgentExecutions('session-a')).toEqual(before)
})
