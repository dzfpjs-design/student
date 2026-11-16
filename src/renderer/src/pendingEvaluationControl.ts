import type { PendingEvaluationReceipt } from './pendingEvaluationReceipt'
import type { AgentTurn } from './types'

export type PendingEvaluationScope = Pick<PendingEvaluationReceipt, 'sessionId' | 'childId' | 'turnId'>

export function freezePendingEvaluation(receipt: PendingEvaluationReceipt | null): PendingEvaluationScope | null {
  return receipt ? { sessionId: receipt.sessionId, childId: receipt.childId, turnId: receipt.turnId } : null
}

// Defense in depth only. The server must enter its restricted lane before interpretation writes.
const controlCapabilities: Readonly<Record<string, string>> = {
  none: 'conversation.respond', describe_plan: 'planning.describe', query_device_status: 'device.status',
  find_parent: 'safety.find_parent', find_parent_urgent: 'safety.find_parent',
  pause_active_task: 'task.pause', resume_paused_task: 'task.resume',
  begin_break: 'break.begin', resume_study: 'break.resume',
  pause_exam_simulation: 'exam.pause', resume_exam_simulation: 'exam.resume',
  pause_observation: 'observation.pause', resume_observation: 'observation.resume',
  stop_speech: 'speech.stop', repeat_response: 'speech.repeat',
  control_document_monitor: 'document.monitor_control', control_tutor_playback: 'tutor.playback_control',
  control_pet_listening: 'pet.listening_control', start_companion_activity: 'companion.start_activity',
  record_companion_activity_feedback: 'companion.activity_feedback',
  inspect_learning_system: 'system.inspect_learning',
  ask_tutor: 'learning.explain',
}

export function acceptsPendingEvaluationControl(turn: AgentTurn, scope: PendingEvaluationScope): boolean {
  const marker = turn.action?.pending_evaluation_control
  if (turn.session_id !== scope.sessionId || turn.child_id !== scope.childId || turn.agent_turn_id === scope.turnId
    || marker?.version !== 'pending_evaluation_control_v1' || marker.mode !== 'control_only'
    || marker.evaluation_turn_id !== scope.turnId || !['pending', 'completed'].includes(turn.tool_status) || turn.after_execution === 'replan'
    || (!['server', 'client_device_bridge'].includes(turn.runtime_execution_location ?? '')
      && !(turn.action.type === 'ask_tutor' && turn.runtime_execution_location === 'local_model'))) return false
  const capability = controlCapabilities[turn.action.type]
  if (!capability || turn.runtime_capability !== capability
    || (turn.action.capability !== undefined && turn.action.capability !== capability)) return false
  if (turn.action.type === 'control_document_monitor' && !['pause', 'resume'].includes(turn.action.command ?? '')) return false
  if (turn.action.type === 'ask_tutor' && (turn.runtime_execution_location !== 'local_model'
    || turn.after_execution !== 'stop' || turn.action.source !== 'agent_runtime_planner'
    || turn.action.local_only !== true || turn.action.cloud_transmission_allowed !== false
    || turn.action.question !== turn.transcript || !turn.action.question?.trim() || !turn.action.subject?.trim()
    || !/^foreground-question:[0-9a-f]{64}$/.test(turn.action.tutor_request_action_id ?? '')
    || turn.action.diagnostic_prompt_id || turn.action.diagnostic_item_version || turn.action.ocr_confirmation_id)) return false
  if (turn.action.type === 'inspect_learning_system' && (turn.runtime_execution_location !== 'server'
    || turn.after_execution !== 'stop' || turn.action.local_only !== true || turn.action.cloud_transmission_allowed !== false
    || !['resources', 'plan', 'memory', 'progress', 'requests', 'all'].includes(turn.action.scope ?? '')) ) return false
  const query = turn.action.pending_evaluation_query
  return query === undefined || (turn.action.type === 'none' && query.turn_id === scope.turnId)
}
