import { expect, it } from 'vitest'
import { freezePendingEvaluation, acceptsPendingEvaluationControl } from './pendingEvaluationControl'
import type { AgentTurn } from './types'

const scope = { sessionId: 'session-a', childId: 'child-a', turnId: 'evaluation-a' }
const reply = (): AgentTurn => ({ session_id: scope.sessionId, child_id: scope.childId, agent_turn_id: 'control-a',
  transcript: '暂停一下', intent: 'pause_task', response_text: '先暂停', tool_status: 'pending',
  tool_error_code: null, created_at: '2026-09-09T00:00:00Z', completed_at: null, orchestrator_version: 'test',
  runtime_capability: 'task.pause', runtime_execution_location: 'server', action: { type: 'pause_active_task',
    pending_evaluation_control: { version: 'pending_evaluation_control_v1', evaluation_turn_id: scope.turnId, mode: 'control_only' } } })

it('freezes evaluation identity by value, not the later receipt reference', () => {
  const receipt = { ...scope, version: 1 as const, status: 'unknown' as const, silent: false }
  const frozen = freezePendingEvaluation(receipt)
  receipt.turnId = 'evaluation-b'
  expect(frozen).toEqual(scope)
  expect(freezePendingEvaluation(null)).toBeNull()
})

it('admits only exact child/session/restricted original plus a matching non-scoring action', () => {
  expect(acceptsPendingEvaluationControl(reply(), scope)).toBe(true)
  for (const patch of [{ child_id: 'child-b' }, { session_id: 'session-b' }, { agent_turn_id: scope.turnId },
    { runtime_capability: 'learning.evaluate_response' }, { runtime_execution_location: 'local_model' as const }]) {
    expect(acceptsPendingEvaluationControl({ ...reply(), ...patch }, scope)).toBe(false)
  }
  const value = reply()
  value.action.pending_evaluation_control!.evaluation_turn_id = 'evaluation-b'
  expect(acceptsPendingEvaluationControl(value, scope)).toBe(false)
  delete value.action.pending_evaluation_control
  expect(acceptsPendingEvaluationControl(value, scope)).toBe(false)
})

it.each(['ask_tutor', 'submit_diagnostic_answer', 'submit_exam_answer', 'submit_pet_vocabulary_answer',
  'submit_mistake_answer', 'continue_mistake_dialogue', 'record_tutor_feedback', 'confirm_writing_original',
  'complete_mistake_remediation', 'unknown_future_action'])('rejects score-capable or unknown %s despite a marker', type => {
  const value = reply(); value.action.type = type
  expect(acceptsPendingEvaluationControl(value, scope)).toBe(false)
})

it('accepts only read-only original status query and rejects a mismatched query identity', () => {
  const value = reply()
  value.action.type = 'none'; value.runtime_capability = 'conversation.respond'; value.tool_status = 'completed'
  value.action.pending_evaluation_query = { turn_id: scope.turnId }
  expect(acceptsPendingEvaluationControl(value, scope)).toBe(true)
  value.action.pending_evaluation_query.turn_id = 'evaluation-b'
  expect(acceptsPendingEvaluationControl(value, scope)).toBe(false)
})

it('does not require cloud authorization for a scoped local safety action', () => {
  const value = reply()
  value.intent = 'urgent_safety_support'
  value.runtime_capability = 'safety.find_parent'
  value.runtime_execution_location = 'client_device_bridge'
  value.action = { type: 'find_parent_urgent', local_only: true, cloud_transmission_allowed: false,
    pending_evaluation_control: value.action.pending_evaluation_control }
  expect(acceptsPendingEvaluationControl(value, scope)).toBe(true)
  expect(acceptsPendingEvaluationControl({ ...value, child_id: 'other-child' }, scope)).toBe(false)
  delete value.action.pending_evaluation_control
  expect(acceptsPendingEvaluationControl(value, scope)).toBe(false)
})

it('admits a server-owned read-only resource query without reopening scoring', () => {
  const value = reply()
  value.action = { type: 'inspect_learning_system', scope: 'resources', track: 'pet',
    local_only: true, cloud_transmission_allowed: false, pending_evaluation_control: value.action.pending_evaluation_control }
  value.runtime_capability = 'system.inspect_learning'
  value.after_execution = 'stop'
  expect(acceptsPendingEvaluationControl(value, scope)).toBe(true)
  for (const action of [{ ...value.action, scope: 'evaluate' }, { ...value.action, local_only: false },
    { ...value.action, cloud_transmission_allowed: true }]) {
    expect(acceptsPendingEvaluationControl({ ...value, action }, scope)).toBe(false)
  }
  expect(acceptsPendingEvaluationControl({ ...value, after_execution: 'replan' }, scope)).toBe(false)
  expect(acceptsPendingEvaluationControl({ ...value, runtime_execution_location: 'client_device_bridge' }, scope)).toBe(false)
})

it('admits only the exact independent tutor operation without original-source or scoring permission', () => {
  const value = reply()
  value.transcript = '数学里两个负数相乘为什么得到正数？'
  value.action = { type: 'ask_tutor', source: 'agent_runtime_planner', question: value.transcript,
    subject: '数学', hint_level: 6, tutor_request_action_id: 'foreground-question:' + 'a'.repeat(64),
    local_only: true, cloud_transmission_allowed: false, pending_evaluation_control: value.action.pending_evaluation_control }
  value.runtime_capability = 'learning.explain'
  value.runtime_execution_location = 'local_model'
  value.after_execution = 'stop'
  expect(acceptsPendingEvaluationControl(value, scope)).toBe(true)
  for (const patch of [{ question: 'different' }, { source: 'legacy' }, { local_only: false },
    { cloud_transmission_allowed: true }, { tutor_request_action_id: 'new-unbound' },
    { diagnostic_prompt_id: 'original' }, { ocr_confirmation_id: 'original-page' }]) {
    expect(acceptsPendingEvaluationControl({ ...value, action: { ...value.action, ...patch } }, scope)).toBe(false)
  }
  expect(acceptsPendingEvaluationControl({ ...value, after_execution: 'replan' }, scope)).toBe(false)
  expect(acceptsPendingEvaluationControl({ ...value, runtime_execution_location: 'server' }, scope)).toBe(false)
})
