import { afterEach, describe, expect, it, vi } from 'vitest'
import { parseTutorToolRecovery, parentHelpMessage, TUTOR_RECOVERY_MAX_AGE_MS, tutorFailureMessage, tutorSourceBlock, tutorSourceBlockMessage } from './agentToolRecovery'

afterEach(() => vi.useRealTimers())

describe('tutor tool recovery', () => {
  it('preserves the original follow-up source for a retry and rejects mixed sources', () => {
    const saved = { version: 1, turnId: 'agent-turn', actionId: 'agent-turn-followup', subject: '数学',
      question: '为什么取倒数？', followupSourceTurnId: 'tutor-original', createdAt: new Date().toISOString(), attempts: 1 }
    expect(parseTutorToolRecovery(JSON.parse(JSON.stringify(saved)))).toMatchObject({ followupSourceTurnId: 'tutor-original' })
    expect(parseTutorToolRecovery({ ...saved, parentTurnId: 'other' })).toBeNull()
    expect(parseTutorToolRecovery({ ...saved, ocrConfirmationId: 'other' })).toBeNull()
    expect(tutorSourceBlock('TUTOR_FOLLOWUP_SOURCE_UNAVAILABLE')).toBe('changed')
  })
  it('restores a rejected independent reply without offering model retry', () => {
    const saved = { version: 1, turnId: 'independent-turn', actionId: 'foreground-question:original', subject: '数学',
      question: '为什么负负得正', createdAt: new Date().toISOString(), attempts: 1,
      sourceBlocked: tutorSourceBlock('FOREGROUND_TUTOR_REPLY_SCOPE_CHANGED'), requiresExplicitRetryConfirmation: true }
    expect(parseTutorToolRecovery(JSON.parse(JSON.stringify(saved)))).toMatchObject({
      sourceBlocked: 'foreground_reply', requiresExplicitRetryConfirmation: false,
    })
    expect(tutorSourceBlockMessage(saved.sourceBlocked)).toContain('原题和问题都保留')
    expect(tutorSourceBlockMessage(saved.sourceBlocked)).not.toContain('重试')
  })
  it('retains the exact OCR confirmation across recovery and rejects mixed question authorities', () => {
    const saved = { version: 1, turnId: 'document-teach', actionId: 'document-goal:original:tutor', subject: '数学',
      question: '原题', createdAt: new Date().toISOString(), attempts: 1, ocrConfirmationId: 'confirmed-original' }
    expect(parseTutorToolRecovery(JSON.parse(JSON.stringify(saved)))).toMatchObject({ ocrConfirmationId: 'confirmed-original' })
    expect(parseTutorToolRecovery({ ...saved, ocrConfirmationId: '' })).toBeNull()
    expect(parseTutorToolRecovery({ ...saved, ocrConfirmationId: 7 })).toBeNull()
    expect(parseTutorToolRecovery({ ...saved, questionReference: { prompt_id: 'other', item_version: 'other' } })).toBeNull()
  })
  it('preserves exact question identity on retry and rejects partial references', () => {
    const saved = { version: 1, turnId: 'turn-question', actionId: 'question-action', subject: '数学', question: '计算这道题', createdAt: new Date().toISOString(), attempts: 1,
      questionReference: { prompt_id: 'original-prompt', item_version: 'original-version' } }
    expect(parseTutorToolRecovery(JSON.parse(JSON.stringify(saved)))?.questionReference).toEqual(saved.questionReference)
    expect(parseTutorToolRecovery({ ...saved, questionReference: { prompt_id: 'original-prompt' } })).toBeNull()
    expect(parseTutorToolRecovery({ ...saved, questionReference: { ...saved.questionReference, item_version: 1 } })).toBeNull()
  })
  it('retains a global hint parent and action type across reload instead of retrying it as a new question', () => {
    const saved = { version: 1, turnId: 'turn-hint', actionId: 'hint-action', subject: '数学', question: '为什么负负得正', createdAt: new Date().toISOString(), attempts: 1,
      parentTurnId: 'prior-turn', actionType: 'more_tutor_hint', hintLevel: 2 }
    expect(parseTutorToolRecovery(JSON.parse(JSON.stringify(saved)))).toMatchObject({ parentTurnId: 'prior-turn', actionType: 'more_tutor_hint', hintLevel: 2 })
    expect(parseTutorToolRecovery({ ...saved, parentTurnId: undefined })).toBeNull()
    expect(parseTutorToolRecovery({ ...saved, actionType: 'delete_records' })).toBeNull()
  })
  it('restores only bounded, versioned read-only tutor recovery metadata', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-28T12:00:00Z'))
    expect(parseTutorToolRecovery({
      version: 1, turnId: 'turn-1', actionId: 'tutor-action-1', subject: '数学', question: '这题为什么这样做？', hintLevel: 1, createdAt: '2026-08-28T00:00:00Z', attempts: 1, requiresExplicitRetryConfirmation: false
    })).toEqual({
      version: 1, turnId: 'turn-1', actionId: 'tutor-action-1', subject: '数学', question: '这题为什么这样做？', hintLevel: 1, createdAt: '2026-08-28T00:00:00Z', attempts: 1, requiresExplicitRetryConfirmation: false
    })
    expect(parseTutorToolRecovery({ version: 1, turnId: 'turn-1', actionId: 'tutor-action-1', subject: '数学', question: '', createdAt: 'now', attempts: 1 })).toBeNull()
    expect(parseTutorToolRecovery({ version: 1, turnId: 'turn-1', actionId: 'tutor-action-1', subject: '数学', question: '题目', createdAt: 'now', attempts: 4 })).toBeNull()
    expect(parseTutorToolRecovery({ version: 1, turnId: 'turn-1', actionId: 'tutor-action-1', subject: '数学', question: '题目', createdAt: 'not-a-date', attempts: 1 })).toBeNull()
    expect(parseTutorToolRecovery({ version: 1, turnId: 'turn-1', actionId: 'tutor-action-1', subject: '数学', question: '题目', createdAt: new Date(Date.now() - TUTOR_RECOVERY_MAX_AGE_MS - 1).toISOString(), attempts: 1 })).toBeNull()
    expect(parseTutorToolRecovery({ version: 1, turnId: 'turn-1', actionId: 'tutor-action-1', subject: '数学', question: '题目', createdAt: '2026-08-29T12:10:00Z', attempts: 1 })).toBeNull()
  })

  it('keeps the child message actionable without surfacing transport details', () => {
    expect(tutorFailureMessage(1)).toContain('重试讲解')
    expect(tutorFailureMessage(2)).toContain('家长')
    expect(parentHelpMessage()).toContain('家长中心')
    expect(parentHelpMessage()).not.toContain('JSON')
  })

  it('keeps bounded local-only course evidence across recovery serialization and rejects unsafe policy flags', () => {
    const courseContext = { status: 'matched', cloud_transmission_allowed: false, mastery_update_allowed: false, diagnostic_prompt: '核对本地课程', evidence: [] }
    const saved = { version: 1, turnId: 'turn-course', actionId: 'action-course', subject: '数学', question: '为什么先列式？', createdAt: new Date().toISOString(), attempts: 1, courseContext }
    expect(parseTutorToolRecovery(JSON.parse(JSON.stringify(saved)))?.courseContext).toEqual(courseContext)
    expect(parseTutorToolRecovery({ ...saved, courseContext: { ...courseContext, cloud_transmission_allowed: true } })).toBeNull()
    expect(parseTutorToolRecovery({ ...saved, courseContext: { ...courseContext, mastery_update_allowed: true } })).toBeNull()
  })
})
