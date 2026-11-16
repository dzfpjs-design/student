import { ApiError, localApi } from './api'
import { retainedListeningReply } from './listeningDeferredReply'
import { isSchoolOpenReview, loadBoundReview, savedSchoolReviewReply } from './boundReview'
import { effectiveActiveMinutes } from './studyTime'
import { isPetListening } from './petListeningPlayback'
import { DIAGNOSTIC_TASK_START_MESSAGE } from './diagnosticTaskStart'
import { clearDiagnosticCaptureForPrompt } from './voiceCaptureContext'
import { VoiceActionUnavailableError } from './voiceJourneyRuntime'
import { writingFeedbackHoldMessage } from './writingFeedbackContinuation'
import type { DiagnosticPromptResult, SessionWorkspaceState } from './types'

export async function runDiagnosticVoice(
  workspace: SessionWorkspaceState,
  action: { type: 'start_diagnostic' | 'replay_diagnostic' | 'submit_diagnostic_answer' | 'cancel_diagnostic'; track?: string; subject?: string; knowledge_node_id?: string; require_unexposed?: boolean; answer?: string; diagnostic_prompt_id?: string | null; diagnostic_item_version?: string | null },
  actionId: string,
  isCurrent: () => boolean,
  originalClientAdmission = true,
): Promise<{ response: string; result: DiagnosticPromptResult; playbackAllowed?: boolean }> {
  const sid = workspace.session.session_id
  const guard = (): void => { if (!isCurrent()) throw new Error('这次题目操作已暂停，未继续执行。') }
  if (['submit_diagnostic_answer', 'cancel_diagnostic'].includes(action.type) && (!action.diagnostic_prompt_id || !action.diagnostic_item_version)) {
    throw new VoiceActionUnavailableError('听到这句话时没有绑定待答题，我不会把答案记到后来出现的题目上。')
  }
  let result = await localApi.diagnosticPrompt(sid, action.diagnostic_prompt_id ?? undefined)
  guard()
  if (action.diagnostic_prompt_id && (result.prompt?.prompt_id !== action.diagnostic_prompt_id || result.prompt?.item_version !== action.diagnostic_item_version)) {
    throw new VoiceActionUnavailableError('这次话语对应的题目版本已变化，未把回答或取消操作应用到另一道题。')
  }
  if (result.status === 'invalidated' && result.reason === 'source_withdrawn') {
    if (result.prompt) clearDiagnosticCaptureForPrompt(sid, result.prompt)
    return { response: '这道题的材料已撤回，之前保存的回答还在。这项复查会保留，等合适的新题补齐后再问你是否继续。你也可以告诉我先做别的。', result }
  }
  const taskStartRecovery = () => {
    if (!result.prompt || result.prompt.child_id !== workspace.session.child_id || result.prompt.origin_session_id !== sid) {
      throw new VoiceActionUnavailableError('原任务题目与当前孩子或会话不一致，未更改其他题目。')
    }
    clearDiagnosticCaptureForPrompt(sid, result.prompt)
    return { response: DIAGNOSTIC_TASK_START_MESSAGE, result }
  }
  if (result.status === 'awaiting_task_start') return taskStartRecovery()
  if (action.type === 'cancel_diagnostic') {
    if (result.prompt && result.status === 'waiting_answer') {
      result = await localApi.cancelDiagnosticPrompt(sid, result.prompt)
      guard()
    }
    return { response: result.status === 'cancelled' ? '这道题已取消。已经保存的学习记录仍然保留。'
      : result.status === 'answer_saved' ? '原回答已经保存，不能当作未答题取消。可以先核对结果。'
      : '目前没有等待作答的题目，没有删除学习记录。', result }
  }
  const independentChallenge = action.type === 'start_diagnostic' && action.require_unexposed === true
  const activeReview = independentChallenge ? undefined : workspace.plan_items.find(item => item.status === 'active' && item.source_type === 'due_review')
  const schoolReview = activeReview && result.prompt?.item.assessment_mode === 'open_response_rubric'
    ? await loadBoundReview(activeReview, sid, workspace.session.child_id) : null
  guard()
  if (schoolReview && result.status === 'answered') {
    const retained = savedSchoolReviewReply(schoolReview, result)
    if (retained) { clearDiagnosticCaptureForPrompt(sid, result.prompt!); return { response: retained, result } }
  }
  if (action.type === 'start_diagnostic' && !['waiting_answer', 'answer_saved'].includes(result.status)) {
    const bound = activeReview ? await loadBoundReview(activeReview, sid, workspace.session.child_id) : null
    guard()
    if (bound?.review_status === 'completed') return { response: '这项复测的回答已经保存，可以核对结果后继续下一项。', result }
    if (originalClientAdmission && !bound) {
      guard()
      let admission: { status: string }
      try { admission = await localApi.admitOriginalClientExecution(sid, actionId) }
      catch {
        guard()
        throw new VoiceActionUnavailableError('这次出题还没有确认，学习进度已保留。请再说一次刚才的要求，我会先找回已保存的题目。')
      }
      guard()
      if (admission.status !== 'admitted') {
        throw new VoiceActionUnavailableError('我还在核对刚才的题目，没有重复出题。学习进度已保留，请再说一次刚才的要求。')
      }
    }
    result = await localApi.prepareDiagnosticPrompt(sid, {
      track: bound?.track ?? action.track ?? 'school', preferred_item_id: bound?.diagnostic_item_id,
      review_task_id: bound?.review_task_id, require_unexposed: !bound, client_action_id: `voice-diagnostic:${actionId}`,
      ...(!bound ? {paper_authorized_turn_id: actionId} : {}),
      ...(independentChallenge && action.subject ? { subject: action.subject } : {}),
      ...(independentChallenge && action.knowledge_node_id ? { knowledge_node_id: action.knowledge_node_id } : {}),
    })
    guard()
  }
  if (result.status === 'awaiting_task_start') return taskStartRecovery()
  if ((result.status === 'answer_saved' || (result.status === 'answered' && action.type !== 'start_diagnostic')) && result.prompt) {
    // Scoring may have committed before review completion or the next prompt.
    // Resume the original receipt so those idempotent consumers also recover.
    result = await localApi.resumeDiagnosticPrompt(sid, result.prompt)
    guard()
  } else if (action.type === 'submit_diagnostic_answer') {
    if (result.status !== 'waiting_answer' || !result.prompt) throw new VoiceActionUnavailableError('目前没有等待作答的题目。我不会把这句话记成另一道题的答案。')
    if (!action.answer?.trim()) throw new VoiceActionUnavailableError('这次没有收到完整答案，请再说一次，也可以说不会。')
    const original = result.prompt
    const listeningReview = Boolean(original.review_task_id && isPetListening(original))
    try {
      result = await localApi.answerDiagnosticPrompt(sid, {
        prompt_id: original.prompt_id, item_version: original.item_version,
        answer: action.answer, client_action_id: `diagnostic-answer:${original.prompt_id}${listeningReview ? `:${actionId}` : ''}`,
        authorized_turn_id: actionId,
      })
    } catch (error) {
      if (!(listeningReview && error instanceof ApiError && error.status === 409
        && error.code === 'DIAGNOSTIC_PROMPT_LISTENING_REVIEW_DELIVERY_REQUIRED')) throw error
      const restored = await localApi.diagnosticPrompt(sid, original.prompt_id)
      guard()
      const message = retainedListeningReply(restored, original)
      if (!message) throw error
      return { response: message, result: restored, playbackAllowed: false }
    }
    guard()
  }
  if (result.status === 'awaiting_task_start') return taskStartRecovery()
  let feedback = result.attempt?.feedback ?? ''
  const held = writingFeedbackHoldMessage(result.local_learning_evaluation)
  if (held && result.status === 'answered' && result.attempt) {
    if (!result.prompt || result.prompt.child_id !== workspace.session.child_id || result.prompt.origin_session_id !== sid
      || result.attempt.child_id !== workspace.session.child_id || result.attempt.session_id !== sid
      || result.attempt.diagnostic_item_id !== result.prompt.diagnostic_item_id) {
      throw new VoiceActionUnavailableError('回答归属尚未核对一致，未继续评价或要求重答。')
    }
    return { response: held, result }
  }
  if (result.foundation_remediation) {
    const next = result.foundation_remediation, original = result.prompt, attempt = result.attempt
    const task = workspace.plan_items.find(item => item.plan_item_id === next.origin.plan_item_id)
    if (result.status !== 'answered' || !original || !attempt || original.child_id !== workspace.session.child_id
      || original.origin_session_id !== sid || attempt.child_id !== original.child_id || attempt.session_id !== sid
      || original.prompt_id !== next.origin.prompt_id || original.item_version !== next.origin.item_version
      || original.diagnostic_item_id !== attempt.diagnostic_item_id || attempt.attempt_id !== next.origin.attempt_id
      || attempt.evidence_id !== next.origin.evidence_id || attempt.score_status !== 'verified_local'
      || attempt.correctness === null || attempt.correctness >= 1 || !next.local_only || next.cloud_transmission_allowed !== false
      || !next.response_text.trim() || !task || task.task_type !== 'goal_prerequisite' || task.status !== 'active') {
      throw new VoiceActionUnavailableError('后续核对与原题、任务或已保存答案不一致，未切换到其他错题。')
    }
    clearDiagnosticCaptureForPrompt(sid, original)
    return { response: next.response_text, result }
  }
  if (result.numeric_clarification && result.prompt && result.attempt) {
    const original = result.prompt, packet = result.numeric_clarification, attempt = result.attempt
    if (original.child_id !== workspace.session.child_id || original.origin_session_id !== sid
      || attempt.child_id !== original.child_id || attempt.session_id !== sid
      || attempt.diagnostic_item_id !== original.diagnostic_item_id || attempt.correctness !== null
      || packet.parent_prompt_id !== original.prompt_id || packet.parent_item_version !== original.item_version
      || packet.parent_attempt_id !== attempt.attempt_id) throw new VoiceActionUnavailableError('原回答与澄清题目未核对一致，未要求重复作答。')
    const next = await localApi.prepareDiagnosticPrompt(sid, { track: original.item.track,
      preferred_item_id: original.diagnostic_item_id, review_task_id: original.review_task_id ?? undefined,
      client_action_id: `numeric-clarification:${attempt.attempt_id}`, numeric_clarification: packet })
    guard()
    if (next.status !== 'waiting_answer' || !next.prompt) return {
      response: '原回答已经保留，这次澄清没有进入待答状态，我们先核对题目。', result: next }
    if (next.prompt.child_id !== original.child_id || next.prompt.origin_session_id !== sid
      || next.prompt.diagnostic_item_id !== original.diagnostic_item_id || next.prompt.item_version !== original.item_version
      || next.prompt.prompt_id === original.prompt_id) throw new VoiceActionUnavailableError('澄清题目已变化，未把下一句话记到其他题目。')
    return { response: [feedback, next.prompt.item.prompt].filter(Boolean).join('。'), result: next }
  }
  if (activeReview && result.attempt && result.attempt.review_task_id === activeReview.source_ref) {
    const receipt = result.attempt
    if (receipt.child_id !== workspace.session.child_id || receipt.session_id !== sid || !receipt.evidence_id) {
      throw new VoiceActionUnavailableError('复测回执与当前孩子或学习会话不一致，没有完成任务。')
    }
    if (schoolReview && isSchoolOpenReview(schoolReview)) {
      const current = await loadBoundReview(activeReview, sid, workspace.session.child_id)
      guard()
      const retained = savedSchoolReviewReply(current, result)
      if (retained) return { response: retained, result }
      if (current.review_status !== 'completed' || current.attempt?.attempt_id !== receipt.attempt_id
        || current.attempt.evidence_id !== receipt.evidence_id) {
        throw new VoiceActionUnavailableError('原稿与当前评分尚未核对一致，回答保留，未完成任务。')
      }
    }
    await localApi.completePlanItem(activeReview.plan_item_id, effectiveActiveMinutes(activeReview), {
      type: 'child_checked', checklist: ['review_completed', 'self_tested'], note: '本项复测已核对同一孩子、会话、题目及实际评分回执。',
    })
    guard()
  }
  if (result.attempt?.next_specialized_item) {
    const item = result.attempt.next_specialized_item
    const next = await localApi.prepareDiagnosticPrompt(sid, {
      track: item.track, preferred_item_id: item.diagnostic_item_id,
      client_action_id: `diagnostic-after:${result.attempt.attempt_id}`,
    })
    guard()
    result = next.status === 'awaiting_task_start' ? next : { ...next, attempt: result.attempt }
  }
  if (result.status === 'awaiting_task_start') return taskStartRecovery()
  if (result.status === 'waiting_answer' && result.prompt) {
    const item = result.prompt.item
    if (isPetListening(result.prompt)) return { response: [feedback, item.prompt].filter(Boolean).join('。'), result }
    return { response: [feedback, item.stimulus_text, item.prompt].filter((text, i, values) => text && values.indexOf(text) === i).join('。'), result }
  }
  if (result.status === 'answered' && result.attempt && ['queued','running'].includes(result.original_audio_teaching?.state ?? '')) {
    return { response: '原回答已经保存，原音练习建议还在处理中。你可以继续说话，也可以先暂停；这不会作为掌握结论。', result }
  }
  if (result.status === 'answered' && result.attempt) return { response: `${feedback}。本次回答已保存，后续还会用不同题目和跨天复测核对掌握程度。`, result }
  if (result.status === 'answer_saved') return { response: '回答已经保存，评分尚未完成。我会保留这次回答，不需要重复作答。', result }
  const paperReasons:Record<string,string>={
    PAPER_SELECTION_REQUIRED:'这次还没有明确选好的试卷。请家长在已发布材料中选本次要做的题，再告诉我开始这张卷。',
    PAPER_SELECTION_SOURCE_CHANGED:'本次材料的来源或发布版本已变化，原回答仍保留。请家长核对并重新选择，我不会换成其他题。',
    PAPER_SELECTION_SOURCE_UNAVAILABLE:'这份材料当前不可用，原回答仍保留。请家长核对发布与来源后重新选择。',
    PAPER_SELECTION_RESTORE_RESELECT_REQUIRED:'数据恢复后需要重新选择本次学习材料，已有回答保留。',
    PAPER_SELECTION_EXHAUSTED:'本次选好的题已经看过，没有新的独立题。请家长补充另一道已审核原题，我不会把旧题当新题。',
    PAPER_SELECTION_ARRIVAL_CHANGED:'你开口后本次材料的选择发生了变化，已有回答保留。请先核对当前材料，再告诉我开始。',
    PAPER_SELECTION_INTENT_UNCLEAR:'我还不能确认你是否要开始本次选好的材料，先保留当前记录。请告诉我现在想做什么。',
    PAPER_SELECTION_SCOPE_CONFLICT:'本次材料与当前题目范围不同，请先核对要做哪份材料。',
    PAPER_SELECTION_VOICE_NOT_EXECUTABLE:'这次选题指令已暂停，原回答保留。请重新告诉我你要做什么。',
    PAPER_SELECTION_VOICE_SUPERSEDED:'刚才的选题指令已被新的话语替代，请重新告诉我你要做什么。',
    PAPER_SELECTION_VOICE_BINDING_CHANGED:'这次选题指令未核对一致，原回答保留。请重新告诉我你要做什么。',
  }
  if(result.reason&&paperReasons[result.reason])return {response:paperReasons[result.reason],result,playbackAllowed:false}
  if (result.reason === 'unexposed_history_unavailable') return { response: '暂时核对不了之前看过哪些题，先不把下一题当作独立新题。已经保存的回答仍保留。', result, playbackAllowed: false }
  if (result.reason === 'unexposed_item_unavailable') return { response: '目前没有核对通过且未见过的新题，之前保存的回答仍保留。我不会把旧题当作新的独立检查。', result, playbackAllowed: false }
  return { response: result.status === 'invalidated' ? '这道题的来源或版本已变化，已停止作答，请重新选题。'
    : result.status === 'cancelled' ? '这道题已经取消，没有等待作答的内容。' : '目前没有匹配且可以使用的诊断题，我不会凭空出题。', result }
}
