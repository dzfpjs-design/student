import { describe, expect, it } from 'vitest'
import { supplyResumeConflictMessage } from './supplyResumeConflict'

describe('voice keeps exact bounded supply rejection instead of urging blind retries', () => {
  it.each(['', 'internal traceback'])('keeps saved-answer guidance for a pending daily check (%s)', message => {
    expect(supplyResumeConflictMessage({ status: 409, code: 'ORAL_DAILY_CHECK_FEEDBACK_REQUIRED', message }))
      .toBe('回答已保存，请先核对本次反馈，再结束复查；不用重新回答。')
  })
  it('preserves the current local HTTP explanation for budget and source', () => {
    for (const code of ['SUPPLY_RESUME_BUDGET_INSUFFICIENT', 'SUPPLY_RESUME_SOURCE_CHANGED', 'SUPPLY_RESUME_ORAL_SOURCE_CHANGED']) {
      const message = '今晚剩余时间不够，原练习已保留，不会延后休息。'
      expect(supplyResumeConflictMessage({ status: 409, code, message })).toBe(message)
    }
  })
  it('does not expose unrelated errors or turn non-conflict success into a blocker', () => {
    expect(supplyResumeConflictMessage({ code: 'SUPPLY_RESUME_SOURCE_CHANGED', message: 'network error' })).toBeNull()
    expect(supplyResumeConflictMessage({ status: 500, code: 'SUPPLY_RESUME_SOURCE_CHANGED', message: 'internal data' })).toBeNull()
    expect(supplyResumeConflictMessage({ status: 409, code: 'OTHER_ERROR', message: 'internal data' })).toBeNull()
    expect(supplyResumeConflictMessage({ status: 409, code: 'TASK_START_LEGACY_REVIEW_INTERNAL', message: 'internal data' })).toBeNull()
    expect(supplyResumeConflictMessage({ status: 500, code: 'TASK_START_LEGACY_REVIEW_CHANGED', message: 'internal data' })).toBeNull()
  })
  it('has bounded understandable guidance if an old server lacks the message', () => {
    expect(supplyResumeConflictMessage({ status: 409, code: 'SUPPLY_RESUME_PLAN_CHANGED', message: '' })).toContain('已保存进度')
  })
  it('allows only known oral followup reasons and a relevant empty-message fallback', () => {
    for (const suffix of ['AUTHORITY_CHANGED', 'COMMAND_CHANGED', 'GOAL_CHANGED', 'CONSENT_CHANGED',
      'PROMPT_CHANGED', 'VOICE_CHANGED', 'SOURCE_CHANGED', 'CANCELLED', 'FAILED_COMMAND', 'COMMAND_REQUIRED']) {
      expect(supplyResumeConflictMessage({ status: 409, code: `ANSWER_ORAL_FOLLOWUP_${suffix}`,
        message: '原口语练习需要重新确认。' })).toBe('原口语练习需要重新确认。')
    }
    expect(supplyResumeConflictMessage({ status: 409, code: 'ANSWER_ORAL_FOLLOWUP_SOURCE_CHANGED', message: '' })).toContain('口语')
    expect(supplyResumeConflictMessage({ status: 409, code: 'ANSWER_ORAL_FOLLOWUP_INTERNAL', message: 'private' })).toBeNull()
  })
})
