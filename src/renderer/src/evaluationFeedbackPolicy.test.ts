import { describe, expect, it } from 'vitest'
import { evaluationFeedbackPolicy } from './evaluationFeedbackPolicy'

const current = () => ({
  objective_update_applied: false, feedback_delivery_allowed: true, feedback_status: 'ready',
  local_only: true, cloud_transmission_allowed: false,
  oral_feedback: { version: 'oral_teaching_v2', status: 'content_self_check',
    local_only: true, cloud_transmission_allowed: false, mastery_claim_allowed: false,
    prompt_id: 'original-prompt', item_version: 'item-version',
    feedback_version: 'feedback-version', source_version: 'source-version' },
})

describe('current feedback is distinct from mastery updates', () => {
  it.each(['content_self_check', 'reviewed_audio_feedback'])('allows current %s locally without changing a score', status => {
    const result = current()
    result.oral_feedback.status = status
    expect(evaluationFeedbackPolicy(result)).toEqual({ deliver: true, localOnly: true })
    expect(result.objective_update_applied).toBe(false)
    expect(result.oral_feedback.mastery_claim_allowed).toBe(false)
  })
  it.each([
    { feedback_delivery_allowed: false }, { feedback_delivery_allowed: 'true' },
    { feedback_status: 'blocked' }, { oral_feedback: undefined },
    { objective_update_applied: true }, { local_only: false }, { cloud_transmission_allowed: true },
  ])('does not bypass an invalid or denied projection %j', delta => {
    expect(evaluationFeedbackPolicy({ ...current(), ...delta }).deliver).toBe(false)
  })
  it.each([
    { version: 'unknown' }, { status: 'unavailable' }, { prompt_id: '' }, { source_version: null },
    { item_version: '' }, { feedback_version: '' }, { mastery_claim_allowed: true },
    { local_only: false }, { cloud_transmission_allowed: true },
  ])('requires the original bounded feedback %j', delta => {
    const result = current()
    expect(evaluationFeedbackPolicy({ ...result, oral_feedback: { ...result.oral_feedback, ...delta } }).deliver).toBe(false)
  })
  it('preserves legacy current feedback and silent obsolete goals', () => {
    expect(evaluationFeedbackPolicy({ objective_update_applied: true }).deliver).toBe(true)
    expect(evaluationFeedbackPolicy({ objective_update_applied: false }).deliver).toBe(false)
    expect(evaluationFeedbackPolicy({ formative_only: true, local_only: true })).toEqual({ deliver: true, localOnly: true })
  })
})
