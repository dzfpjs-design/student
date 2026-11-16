import { afterEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { answerBoundReview, loadBoundReview } from './boundReview'
import type { PlanItem } from './types'

describe('bound planned retest', () => {
  afterEach(() => vi.restoreAllMocks())
  const plan = { plan_item_id: 'plan-1', source_type: 'due_review', source_ref: 'review-1' } as PlanItem
  const review = { plan_item_id: 'plan-1', session_id: 'session-a', child_id: 'child-a', review_task_id: 'review-1', review_status: 'pending', attempt: null, diagnostic_item_id: 'question-1', prompt: '这道题有几个解？' }
  const attempt = { attempt_id: 'answer-1', evidence_id: 'evidence-1', child_id: 'child-a', session_id: 'session-a', diagnostic_item_id: 'question-1', review_task_id: 'review-1', feedback: '这次还需要再试', correctness: 0, submitted_answer: '不会', created_at: '2026-09-05T00:00:00Z' }
  it('uses the exact question and receipt for the active plan rather than another due question', async () => {
    vi.spyOn(localApi, 'planReviewQuestion').mockResolvedValue(review as never)
    const send = vi.spyOn(localApi, 'answerDiagnostic').mockResolvedValue(attempt)
    expect(await answerBoundReview(plan, 'session-a', 'child-a', '不会', () => true)).toEqual(attempt)
    expect(send).toHaveBeenCalledExactlyOnceWith('session-a', 'question-1', '不会', 'review-1')
  })
  it('recovers a committed response without submitting the answer twice', async () => {
    vi.spyOn(localApi, 'planReviewQuestion').mockResolvedValueOnce(review as never).mockResolvedValue({ ...review, review_status: 'completed', attempt } as never)
    const send = vi.spyOn(localApi, 'answerDiagnostic').mockRejectedValue(new Error('response lost'))
    expect(await answerBoundReview(plan, 'session-a', 'child-a', '不会', () => true)).toEqual(attempt)
    expect(await answerBoundReview(plan, 'session-a', 'child-a', '不会', () => true)).toEqual(attempt)
    expect(send).toHaveBeenCalledOnce()
  })
  it('rejects cross-child and stale plan bindings before grading', async () => {
    vi.spyOn(localApi, 'planReviewQuestion').mockResolvedValue({ ...review, child_id: 'child-b' } as never)
    const send = vi.spyOn(localApi, 'answerDiagnostic')
    await expect(loadBoundReview(plan, 'session-a', 'child-a')).rejects.toThrow('不匹配')
    expect(send).not.toHaveBeenCalled()
  })
  it('does not submit blank or cancelled answers', async () => {
    vi.spyOn(localApi, 'planReviewQuestion').mockResolvedValue(review as never)
    const send = vi.spyOn(localApi, 'answerDiagnostic')
    await expect(answerBoundReview(plan, 'session-a', 'child-a', '', () => true)).rejects.toThrow('先说出')
    await expect(answerBoundReview(plan, 'session-a', 'child-a', '两个', () => false)).rejects.toThrow('暂停')
    expect(send).not.toHaveBeenCalled()
  })
  it('does not send a school written-review answer through the unbound legacy grading route', async () => {
    vi.spyOn(localApi, 'planReviewQuestion').mockResolvedValue({ ...review, assessment_mode: 'open_response_rubric', review_interval: 'school_open_near' } as never)
    vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue({ status: 'waiting_answer', prompt: null, attempt: null } as never)
    const send = vi.spyOn(localApi, 'answerDiagnostic')
    await expect(answerBoundReview(plan, 'session-a', 'child-a', '我算好了', () => true)).rejects.toThrow('书面解答')
    expect(send).not.toHaveBeenCalled()
  })
  it.each(['child_id', 'session_id', 'review_task_id', 'diagnostic_item_id', 'evidence_id'])('rejects a mismatched first response %s', async (field) => {
    vi.spyOn(localApi, 'planReviewQuestion').mockResolvedValue(review as never)
    vi.spyOn(localApi, 'answerDiagnostic').mockResolvedValue({ ...attempt, [field]: field === 'evidence_id' ? '' : 'wrong' })
    await expect(answerBoundReview(plan, 'session-a', 'child-a', '不会', () => true)).rejects.toThrow('不匹配')
  })
})
