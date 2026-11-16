import type { localApi } from './api'
import type { WritingFeedbackNotice } from './types'
import { evaluationFeedbackPolicy } from './evaluationFeedbackPolicy'

type Execution = Awaited<ReturnType<typeof localApi.executeAgentTurn>>

export function writingFeedbackHoldMessage(notice?: WritingFeedbackNotice): string | null {
  if (notice?.status !== 'blocked' || notice.local_only !== true || notice.cloud_transmission_allowed !== false
    || !['scheduled_break_control_changed', 'scheduled_break_origin_changed'].includes(notice.reason ?? '')) return null
  return '你的回答已经保存了，不用再答一遍。我需要先核对刚才的休息记录，这次先不评价对错。'
}

export async function continueWritingFeedback(input: {
  notice?: WritingFeedbackNotice; sourceTurnId?: string; childId: string; sessionId: string;
  isCurrent: () => boolean; execute: (turnId: string) => Promise<Execution>;
  recover?: (turnId: string) => Promise<Execution | null>;
  beforeEvaluation: (turnId: string) => void; finishEvaluation: (turnId: string) => void;
}): Promise<Execution | null> {
  const notice = input.notice
  if (!notice || !['feedback_required', 'local_feedback_required','feedback_ready','audio_feedback_pending'].includes(notice.status)) return null
  const local = notice.status === 'local_feedback_required'
  const ready = notice.status === 'feedback_ready'
  const target = local ? notice.local_feedback_turn_id : notice.feedback_turn_id
  if (!target) return null
  if (ready && !input.recover) return null
  if (!input.isCurrent()) throw new Error('这道题的反馈已暂停，未执行旧任务。')
  if (notice.local_only !== true || notice.cloud_transmission_allowed !== false || target === input.sourceTurnId) {
    throw new Error('这道题的反馈没有可靠的后续任务绑定。')
  }
  if (notice.status === 'audio_feedback_pending') {
    input.beforeEvaluation(target)
    return null
  }
  if (!local && !ready) input.beforeEvaluation(target)
  const result = ready ? await input.recover!(target) : await input.execute(target)
  if (!input.isCurrent()) throw new Error('这道题的反馈已暂停，未继续旧话题。')
  if (!result) return null
  if (result.turn.agent_turn_id !== target || result.turn.child_id !== input.childId
    || result.turn.session_id !== input.sessionId
    || result.execution.capability !== (local ? 'conversation.respond' : 'learning.evaluate_response')
    || !result.execution.response_text?.trim()) {
    throw new Error('这道题的反馈回执与当前孩子或任务不匹配。')
  }
  if (!local) input.finishEvaluation(target)
  if (!evaluationFeedbackPolicy(result.execution.result).deliver) return null
  return result
}
