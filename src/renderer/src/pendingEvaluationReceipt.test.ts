import { afterEach, describe, expect, it, vi } from 'vitest'
import { clearPendingEvaluation, isExplicitEvaluationTopicChange, readPendingEvaluation, savePendingEvaluation, type PendingEvaluationReceipt } from './pendingEvaluationReceipt'

const receipt: PendingEvaluationReceipt = { version: 1, sessionId: 'session-a', childId: 'child-a', turnId: 'original-turn', status: 'in_progress', silent: false }
afterEach(() => { localStorage.clear(); vi.restoreAllMocks() })

describe('pending learning evaluation identity', () => {
  it('restores the original turn without retaining answer text', () => {
    savePendingEvaluation(receipt)
    expect(readPendingEvaluation('session-a', 'child-a')).toEqual(receipt)
    expect(readPendingEvaluation('another-session', 'child-a')).toBeNull()
    expect(() => readPendingEvaluation('session-a', 'child-b')).toThrow('PENDING_EVALUATION_RECEIPT_INVALID')
    clearPendingEvaluation(receipt)
    expect(readPendingEvaluation('session-a', 'child-a')).toBeNull()
  })

  it('does not clear a newer pending turn when an older result arrives', () => {
    savePendingEvaluation({ ...receipt, turnId: 'newer-turn' })
    clearPendingEvaluation(receipt)
    expect(readPendingEvaluation('session-a', 'child-a')?.turnId).toBe('newer-turn')
  })

  it('surfaces persistence failures before an evaluation may be started', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('storage full') })
    expect(() => savePendingEvaluation(receipt)).toThrow('storage full')
  })

  it('never interprets a retry as explicit topic replacement', () => {
    expect(isExplicitEvaluationTopicChange('重试')).toBe(false)
    expect(isExplicitEvaluationTopicChange('再试一次')).toBe(false)
    expect(isExplicitEvaluationTopicChange('换个话题，聊一下英语')).toBe(true)
    expect(isExplicitEvaluationTopicChange('换题')).toBe(true)
  })
})
