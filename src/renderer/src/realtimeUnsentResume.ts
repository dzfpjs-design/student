import { acknowledgeRealtimeAdmission, pendingRealtimeAdmissions, updateRealtimeAdmission, type RealtimeAdmissionHandoff } from './realtimeAdmissionJournal'
import { pendingAgentRequests } from './agentRequestJournal'
import { pendingAgentExecutions } from './agentExecutionJournal'
import type { AgentTurn } from './types'
import { withReceiptRecoveryWindow } from './receiptRecoveryWindow'
import { acceptsPendingEvaluationControl } from './pendingEvaluationControl'

export type RealtimeResumePreview = {
  session_id: string; child_id: string; input_id: string; voice_input_id: string | null;
  status: 'ready' | 'blocked'; resume_allowed: boolean; response_text: string;
  local_only: true; cloud_transmission_allowed: false; replay_allowed: false;
  mastery_update_allowed: false; raw_audio_available: false;
  resume_kind?: 'execution'; agent_turn_id?: string;
  pending_evaluation_turn_id?: string;
}

export function isRealtimeResumeRequest(text: string): boolean {
  return /^(?:请|帮我)?(?:继续|接着处理|恢复处理)(?:一下)?(?:刚才|上次)(?:保存的|的)?(?:没处理的|未处理的|没发出的|未发出的)?(?:原话|话|语音|请求)[吧。！？!?]?$/u.test(text.trim())
}

export function shouldResumeRealtimeInput(text: string, sessionId: string, childId: string): boolean {
  if (!isRealtimeResumeRequest(text)) return false
  if (/(?:没处理的|未处理的|没发出的|未发出的|保存的)/u.test(text)) return true
  try { return pendingRealtimeAdmissions(sessionId, childId).length > 0 }
  catch { return true }
}

export function settleUnsentResumeJournal(handoff: RealtimeAdmissionHandoff): boolean {
  try {
    const actionId = 'realtime-' + handoff.voiceInputId
    if ([...pendingAgentRequests(handoff.sessionId), ...pendingAgentExecutions(handoff.sessionId)]
      .some(item => item.actionId === actionId)) return false
    acknowledgeRealtimeAdmission(handoff)
    return !pendingRealtimeAdmissions(handoff.sessionId, handoff.childId).some(item => item.inputId === handoff.inputId)
  } catch { return false }
}

async function dispatchWithRecoveryDeadline<T>(signal: AbortSignal, dispatch: (signal: AbortSignal) => Promise<T>): Promise<T> {
  const controller = new AbortController()
  let reject!: (reason: Error) => void
  const interrupted = new Promise<never>((_resolve, fail) => { reject = fail })
  const cancel = (): void => { controller.abort(); reject(new DOMException('Resume stopped', 'AbortError')) }
  signal.addEventListener('abort', cancel, { once: true })
  const timer = setTimeout(() => {
    controller.abort()
    reject(Object.assign(new Error('Original resume result is unconfirmed'), { code: 'REALTIME_RESUME_UNCONFIRMED' }))
  }, 180_000)
  try {
    if (signal.aborted) throw new DOMException('Resume stopped', 'AbortError')
    return await Promise.race([dispatch(controller.signal), interrupted])
  } finally { clearTimeout(timer); signal.removeEventListener('abort', cancel); controller.abort() }
}

export async function resumeOneUnsentInput(sessionId: string, childId: string,
  preview: (session: string, child: string, input: string, signal?: AbortSignal) => Promise<RealtimeResumePreview>,
  dispatch: (session: string, child: string, input: string, voice: string, signal?: AbortSignal) => Promise<AgentTurn>,
  current: () => boolean, signal: AbortSignal, currentInputId?: string,
): Promise<{ turn?: AgentTurn; handoff?: RealtimeAdmissionHandoff; message?: string; pendingEvaluationTurnId?: string }> {
  const all = pendingRealtimeAdmissions(sessionId, childId)
  const pending = all
    .filter(item => item.phase !== 'cancel_requested' && item.inputId !== currentInputId)
  if (!pending.length) return { message: all.some(item => item.phase === 'cancel_requested')
    ? '之前原话已请求撤回，尚不能恢复执行；可以核对原请求。' : '没有找到待续接的原话，可以继续当前学习。' }
  // Preserve speech order. A later ready input must not leapfrog an unknown one.
  const item = pending[0]
  const result = await withReceiptRecoveryWindow(current, signal, window =>
    window.read(readSignal => preview(sessionId, childId, item.inputId, readSignal)))
  if (!current() || signal.aborted) return {}
  if (result.session_id !== sessionId || result.child_id !== childId || result.input_id !== item.inputId
    || !['ready', 'blocked'].includes(result.status) || result.resume_allowed !== (result.status === 'ready')
    || result.local_only !== true || result.cloud_transmission_allowed !== false || result.replay_allowed !== false
    || result.mastery_update_allowed !== false || result.raw_audio_available !== false
    || typeof result.response_text !== 'string' || !result.response_text || result.response_text.length > 500
    || result.voice_input_id !== null && !/^[A-Za-z0-9_-]{8,128}$/.test(result.voice_input_id)
    || result.resume_kind !== undefined && (result.resume_kind !== 'execution' || result.status !== 'ready'
      || typeof result.agent_turn_id !== 'string' || !/^[A-Za-z0-9_-]{1,160}$/.test(result.agent_turn_id))
    || result.agent_turn_id !== undefined && result.resume_kind !== 'execution'
    || result.pending_evaluation_turn_id !== undefined && (typeof result.pending_evaluation_turn_id !== 'string'
      || !/^[A-Za-z0-9_-]{1,128}$/.test(result.pending_evaluation_turn_id))
    || item.voiceInputId !== null && item.voiceInputId !== result.voice_input_id) throw Error('Resume preview scope mismatch')
  if (!result.resume_allowed || result.voice_input_id === null) return { message: result.response_text }
  // Verify CAS even for dispatching: cancellation and another owner can change
  // the journal while the readonly preview is in flight.
  const owned = pendingRealtimeAdmissions(sessionId, childId).find(value => value.inputId === item.inputId)
  if (!owned || JSON.stringify(owned) !== JSON.stringify(item)) return {}
  let handoff = item
  if (handoff.phase === 'admitting') handoff = updateRealtimeAdmission(handoff, 'stored', result.voice_input_id)
  if (handoff.phase === 'stored') handoff = updateRealtimeAdmission(handoff, 'dispatching')
  if (!current() || signal.aborted) return {}
  const turn = await dispatchWithRecoveryDeadline(signal,
    dispatchSignal => dispatch(sessionId, childId, item.inputId, result.voice_input_id!, dispatchSignal))
  if (!current() || signal.aborted) return {}
  if (turn.session_id !== sessionId || turn.child_id !== childId || turn.action?.voice_input_id !== result.voice_input_id
    || !turn.agent_turn_id || !turn.transcript?.trim()
    || result.resume_kind === 'execution' && (turn.agent_turn_id !== result.agent_turn_id
      || turn.tool_status !== 'pending' || turn.runtime_execution_location !== 'server'
      || turn.runtime_capability === 'learning.evaluate_response')) throw Error('Resume turn scope mismatch')
  if (result.pending_evaluation_turn_id
      ? !acceptsPendingEvaluationControl(turn, { sessionId, childId, turnId: result.pending_evaluation_turn_id })
      : turn.action.pending_evaluation_control !== undefined) throw Error('Resume evaluation scope mismatch')
  return { turn, handoff, pendingEvaluationTurnId: result.pending_evaluation_turn_id }
}
