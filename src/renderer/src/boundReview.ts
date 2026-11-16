import { localApi } from './api'
import type { DiagnosticAttempt, DiagnosticPromptResult, PlanItem } from './types'

export type BoundReview = Awaited<ReturnType<typeof localApi.planReviewQuestion>>
type ReviewPlan = Pick<PlanItem, 'plan_item_id' | 'source_type' | 'source_ref'>

export function isSchoolOpenReview(review: BoundReview): boolean {
  return review.assessment_mode === 'open_response_rubric'
    && ['school_open_near', 'school_open_far', 'school_open_7d'].includes(review.review_interval ?? '')
}

export function savedSchoolReviewReply(review: BoundReview, result: DiagnosticPromptResult): string | null {
  if (!isSchoolOpenReview(review) || review.review_status === 'completed'
    || !['answered', 'answer_saved'].includes(result.status)) return null
  const prompt = result.prompt
  if (!prompt || prompt.child_id !== review.child_id || prompt.origin_session_id !== review.session_id
    || prompt.review_task_id !== review.review_task_id || prompt.diagnostic_item_id !== review.diagnostic_item_id) {
    throw new Error('复测原稿与当前孩子或题目不匹配，未重新作答。')
  }
  if (result.status === 'answered') validateAttempt(result.attempt, review, review.session_id, review.child_id)
  return '这道复测的原稿已经保存，评分还需要核对，不用重复作答。核对后会接着安排后续练习。'
}

function validateAttempt(attempt: DiagnosticAttempt | null, review: BoundReview, sessionId: string, childId: string): DiagnosticAttempt {
  if (!attempt?.attempt_id || !attempt.evidence_id || attempt.child_id !== childId || attempt.session_id !== sessionId
    || attempt.review_task_id !== review.review_task_id || attempt.diagnostic_item_id !== review.diagnostic_item_id) {
    throw new Error('复测回执与当前孩子或题目不匹配，未确认任务完成。')
  }
  return attempt
}

export async function loadBoundReview(plan: ReviewPlan, sessionId: string, childId: string): Promise<BoundReview> {
  if (plan.source_type !== 'due_review' || !plan.source_ref) throw new Error('当前计划没有绑定复测记录。')
  const review = await localApi.planReviewQuestion(plan.plan_item_id)
  if (review.plan_item_id !== plan.plan_item_id || review.session_id !== sessionId || review.child_id !== childId
    || review.review_task_id !== plan.source_ref || !review.diagnostic_item_id || !review.prompt?.trim()) {
    throw new Error('复测题与当前孩子或计划不匹配，已停止提交。')
  }
  if (review.review_status === 'completed') validateAttempt(review.attempt, review, sessionId, childId)
  return review
}

export async function answerBoundReview(plan: ReviewPlan, sessionId: string, childId: string, answer: string, isCurrent: () => boolean): Promise<DiagnosticAttempt> {
  const review = await loadBoundReview(plan, sessionId, childId)
  if (!isCurrent()) throw new Error('当前复测已暂停，未继续提交。')
  // A reply can have committed even when the previous HTTP response was lost.
  if (review.review_status === 'completed' && review.attempt) return review.attempt
  if (isSchoolOpenReview(review)) {
    const original = await localApi.diagnosticPrompt(sessionId)
    if (!isCurrent()) throw new Error('当前复测已暂停，未继续提交。')
    throw new Error(savedSchoolReviewReply(review, original)
      ?? '这道复测需要你的书面解答。把解答放到高拍仪下，我会核对这份原稿，不把口述进度当成书面答案。')
  }
  if (!answer.trim()) throw new Error('请先说出你的回答，也可以直接说不会。')
  try {
    return validateAttempt(await localApi.answerDiagnostic(sessionId, review.diagnostic_item_id, answer, review.review_task_id), review, sessionId, childId)
  } catch (error) {
    if (isCurrent()) {
      const saved = await loadBoundReview(plan, sessionId, childId).catch(() => null)
      if (saved?.review_status === 'completed' && saved.attempt) return saved.attempt
    }
    throw error
  }
}
