import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { agentContinuationRecoveryMessage, agentWakeResumePrompt } from './agentWakeResumePrompt'
import { AgentObjectiveStatus } from './AgentObjectiveStatus'
import type { AgentRuntimeStatus, SessionWorkspaceState } from './types'

afterEach(cleanup)
const workspace = { session: { session_id: 's', child_id: 'c', state: 'study', status: 'active' }, plan_items: [] } as unknown as SessionWorkspaceState
const runtime = { session_id: 's', active_objective: { objective_id: 'o', objective_type: 'execution', summary: '核对摄像头', status: 'active', success_criteria: [] },
  current_step: { step_id: 'step', capability_name: 'device.status', status: 'planned', plan_status: 'waiting_child', expected_evidence: [] },
  continuation: { status: 'awaiting_receipt', session_id: 's', child_id: 'c', next_turn_id: 'next', replay_allowed: false, local_only: true, cloud_transmission_allowed: false } } as const

it('a wake does not say waiting for the child or resuming execution when a result is unknown', () => {
  const reply = agentWakeResumePrompt(workspace, runtime as unknown as AgentRuntimeStatus, '同学')
  expect(reply).toContain('结果还没有确认')
  expect(reply).not.toContain('刚才在等你回答')
  expect(reply).not.toContain('从保存的位置接着来')
})
it('shows the uncertain result even with no active objective', () => {
  render(<AgentObjectiveStatus runtime={{ ...runtime, active_objective: null } as unknown as AgentRuntimeStatus} />)
  expect(screen.getByText(/结果还没有确认/)).toBeInTheDocument()
  expect(screen.queryByText('等你回答后继续')).not.toBeInTheDocument()
})
it('does not describe another session as the current conversation', () => {
  const reply = agentWakeResumePrompt(workspace, { ...runtime, session_id: 'another' } as unknown as AgentRuntimeStatus, '同学')
  expect(reply).not.toContain('核对摄像头')
  expect(reply).not.toContain('结果还没有确认')
})

it.each([
  { session_id: 'another' }, { child_id: 'another' }, { next_turn_id: undefined },
  { replay_allowed: true }, { local_only: false }, { cloud_transmission_allowed: true },
  { status: 'completed' },
])('does not narrate an unscoped or ineligible receipt: %j', (change) => {
  expect(agentContinuationRecoveryMessage({ ...runtime, continuation: { ...runtime.continuation, ...change } } as unknown as AgentRuntimeStatus, 'c')).toBeNull()
})

it.each(['failed', 'blocked'])('describes a %s receipt without automatically retrying it', (status) => {
  expect(agentContinuationRecoveryMessage({ ...runtime, continuation: { ...runtime.continuation, status } } as unknown as AgentRuntimeStatus, 'c')).toContain('不会自动重试')
})
