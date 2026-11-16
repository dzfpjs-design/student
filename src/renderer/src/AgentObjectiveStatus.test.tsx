import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { AgentObjectiveStatus } from './AgentObjectiveStatus'
import type { AgentRuntimeStatus } from './types'

afterEach(cleanup)
const runtime: AgentRuntimeStatus = {
  session_id: 'synthetic-session',
  active_objective: { objective_id: 'synthetic-goal', objective_type: 'learning',
    summary: '说明判断依据', success_criteria: ['说明条件', '检查结论'], status: 'active' },
  current_step: null,
}

it.each([undefined, 'unknown_future_phase'])('keeps an unknown/legacy learning phase neutral (%s)', phase => {
  render(<AgentObjectiveStatus runtime={{ ...runtime, active_objective: { ...runtime.active_objective!, phase } }} />)
  expect(screen.getByText('学习目标')).toBeInTheDocument()
  expect(screen.queryByText('现在一起完成')).not.toBeInTheDocument()
  expect(screen.getByText('目标标准：说明条件；检查结论')).toBeInTheDocument()
})

it('keeps current execution and waiting instructions', () => {
  render(<AgentObjectiveStatus runtime={{ ...runtime,
    active_objective: { ...runtime.active_objective!, objective_type: 'execution', phase: 'execute' },
    current_step: { step_id: 'step', capability_name: 'conversation.respond', status: 'completed',
      plan_status: 'waiting_child', expected_evidence: [] } }} />)
  expect(screen.getByText('现在一起完成')).toBeInTheDocument()
  expect(screen.getByText('完成标准：说明条件；检查结论')).toBeInTheDocument()
  expect(screen.getByText('等你回答后继续')).toBeInTheDocument()
})

it.each(['paused', 'completed', 'cancelled'])('does not command a %s objective', status => {
  const { container } = render(<AgentObjectiveStatus runtime={{ ...runtime,
    active_objective: { ...runtime.active_objective!, status } }} />)
  expect(container).toBeEmptyDOMElement()
})

it('has no objective when the runtime returns none', () => {
  const { container } = render(<AgentObjectiveStatus runtime={{ ...runtime, active_objective: null }} />)
  expect(container).toBeEmptyDOMElement()
})
