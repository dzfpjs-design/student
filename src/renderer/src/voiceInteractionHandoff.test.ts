import { describe, expect, it } from 'vitest'
import { validateVoiceInteractionHandoff } from './voiceInteractionHandoff'

const scope = { sessionId: 'synthetic-session', childId: 'synthetic-child' }
const handoffId = `voice-interaction-handoff:${'a'.repeat(64)}`
const progress = { childId: scope.childId, version: 1, step: 'deferred', updatedAt: '2026-10-15T10:20:30.123Z' }
const ready = () => ({ session_id: scope.sessionId, child_id: scope.childId, status: 'ready',
  handoff_id: handoffId, agent_action_id: 'agent-a', deferred_progress: progress,
  request: { transcript: '帮我看看这道题', client_action_id: 'original-a', context: { phase: 'naming' } },
  receipt: null, reason: null })

describe('voice interaction handoff proof', () => {
  it('accepts a scoped ready proof without requiring an ASR record', () => {
    expect(validateVoiceInteractionHandoff(ready(), scope)).toEqual(ready())
  })

  it.each([
    { session_id: 'other-session' }, { child_id: 'other-child' }, { handoff_id: 'bad/id' },
    { handoff_id: 'handoff-a' }, { handoff_id: `voice-interaction-handoff:${'A'.repeat(64)}` },
    { handoff_id: `voice-interaction-handoff:${'a'.repeat(63)}` },
    { agent_action_id: '' }, { agent_action_id: 'a'.repeat(161) },
    { deferred_progress: { ...progress, childId: 'other-child' } },
    { deferred_progress: { ...progress, step: 'completed' } },
    { deferred_progress: { ...progress, updatedAt: '2026-10-15T10:20:30Z' } },
    { deferred_progress: { ...progress, updatedAt: '2026-02-30T10:20:30.123Z' } },
    { deferred_progress: null }, { request: null }, { receipt: {} },
    { request: { ...ready().request, transcript: '' } },
    { request: { ...ready().request, voice_input_id: 'wrong/id' } },
    { request: { ...ready().request, context: { phase: 'unknown' } } },
    { status: 'outcome_unknown' },
  ])('rejects contradictory or unscoped proof %j', changed => {
    expect(() => validateVoiceInteractionHandoff({ ...ready(), ...changed }, scope)).toThrow()
  })

  it('checks the expected opaque handoff and stable Agent identity', () => {
    expect(() => validateVoiceInteractionHandoff(ready(), { ...scope, handoffId: 'another' })).toThrow()
    expect(() => validateVoiceInteractionHandoff(ready(), { ...scope, agentActionId: 'another' })).toThrow()
  })

  it('keeps original plaintext absent from non-ready states', () => {
    expect(validateVoiceInteractionHandoff({ ...ready(), status: 'needs_deferral', request: null }, scope).status).toBe('needs_deferral')
    expect(validateVoiceInteractionHandoff({ ...ready(), status: 'none', handoff_id: null, agent_action_id: null,
      deferred_progress: null, request: null }, scope).status).toBe('none')
    expect(() => validateVoiceInteractionHandoff({ ...ready(), status: 'invalidated' }, scope)).toThrow()
  })

  it.each([['received', 'received'], ['interpreted', 'interpreted'], ['completed', 'recorded_completed'],
    ['failed', 'recorded_failed'], ['outcome_unknown', 'unconfirmed']])('accepts a scoped %s receipt without granting replay', (status, receiptStatus) => {
    const receipt = { session_id: scope.sessionId, client_action_id: 'agent-a', status: receiptStatus, response_text: '原请求状态已核对。',
      local_only: true, cloud_transmission_allowed: false, replay_allowed: false, mastery_update_allowed: false }
    const proof = { ...ready(), status, request: null, receipt }
    expect(validateVoiceInteractionHandoff(proof, scope).receipt).toEqual(receipt)
    expect(() => validateVoiceInteractionHandoff({ ...proof, receipt: { ...receipt, replay_allowed: true } }, scope)).toThrow()
    expect(() => validateVoiceInteractionHandoff({ ...proof, receipt: { ...receipt, client_action_id: 'another-action' } }, scope)).toThrow()
    expect(() => validateVoiceInteractionHandoff({ ...proof, receipt: { ...receipt, session_id: 'another-session' } }, scope)).toThrow()
  })
})
