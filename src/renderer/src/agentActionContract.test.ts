import { describe, expect, it } from 'vitest'
import { assertSupportedAgentAction, SUPPORTED_AGENT_ACTION_TYPES } from './agentActionContract'

describe('agent action contract', () => {
  it('rejects an action the child client cannot execute', () => {
    expect(() => assertSupportedAgentAction('future_unimplemented_action')).toThrow(
      'UNSUPPORTED_AGENT_ACTION:future_unimplemented_action'
    )
  })

  it('contains no duplicate action owners', () => {
    expect(new Set(SUPPORTED_AGENT_ACTION_TYPES).size).toBe(SUPPORTED_AGENT_ACTION_TYPES.length)
    expect(() => assertSupportedAgentAction('ask_tutor')).not.toThrow()
    expect(() => assertSupportedAgentAction('none')).not.toThrow()
    expect(() => assertSupportedAgentAction('inspect_learning_system')).not.toThrow()
  })

  it('accepts the executable cloud-selected tutorial restart action', () => {
    expect(() => assertSupportedAgentAction('resume_voice_tutorial')).not.toThrow()
    expect(() => assertSupportedAgentAction('control_pet_listening')).not.toThrow()
  })
  it('admits both server-owned oral practice actions', () => {
    expect(() => assertSupportedAgentAction('retry_oral_answer')).not.toThrow()
    expect(() => assertSupportedAgentAction('check_oral_independently')).not.toThrow()
  })

  it('admits server-owned activity and versioned input revision actions', () => {
    for (const action of ['record_companion_activity_feedback', 'propose_learning_input_revision',
      'select_learning_input_target', 'confirm_learning_input_revision', 'cancel_learning_input_revision', 'review_learning_input_unplaced', 'review_learning_input_reclaimed_scope']) {
      expect(() => assertSupportedAgentAction(action)).not.toThrow()
    }
  })
})
