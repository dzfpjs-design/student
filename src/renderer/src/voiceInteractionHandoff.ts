import type { VoiceInteractionDeferredProgress, VoiceInteractionHandoff, VoiceInteractionHandoffStatus } from './types'

const identity = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9_-]{1,160}$/.test(value)
const handoffIdentity = (value: unknown): value is string => typeof value === 'string' && /^voice-interaction-handoff:[0-9a-f]{64}$/.test(value)
const object = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value)
const keys = (value: Record<string, unknown>, expected: string[]): boolean => Object.keys(value).sort().join(',') === [...expected].sort().join(',')
const canonicalTimestamp = (value: unknown): value is string => {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(value)) return false
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp) && new Date(timestamp).toISOString() === value
}

export function validateDeferredVoiceOnboardingProgress(value: unknown, childId: string): VoiceInteractionDeferredProgress {
  if (!object(value) || !keys(value, ['childId', 'version', 'step', 'updatedAt']) || value.childId !== childId
    || value.version !== 1 || value.step !== 'deferred' || !canonicalTimestamp(value.updatedAt)) throw new Error('INVALID_HANDOFF_DEFERRED_PROGRESS')
  return value as VoiceInteractionDeferredProgress
}

/** Server proof authorizes one scoped handoff; browser journals contain identities only. */
export function validateVoiceInteractionHandoff(value: unknown, scope: {
  sessionId: string; childId: string; handoffId?: string; agentActionId?: string
}): VoiceInteractionHandoff {
  const fail = (): never => { throw new Error('VOICE_INTERACTION_HANDOFF_SCOPE_MISMATCH') }
  if (!identity(scope.sessionId) || !identity(scope.childId) || !object(value)
    || !keys(value, ['session_id', 'child_id', 'status', 'handoff_id', 'agent_action_id', 'deferred_progress', 'request', 'receipt', 'reason'])
    || value.session_id !== scope.sessionId || value.child_id !== scope.childId
    || typeof value.status !== 'string' || !['none', 'needs_deferral', 'ready', 'received', 'interpreted', 'completed', 'failed', 'outcome_unknown', 'invalidated'].includes(value.status)
    || value.reason !== null && (typeof value.reason !== 'string' || value.reason.length > 1000)) return fail()
  const status = value.status as VoiceInteractionHandoffStatus
  if (status === 'none') {
    if (value.handoff_id !== null || value.agent_action_id !== null || value.deferred_progress !== null || value.request !== null || value.receipt !== null || value.reason !== null
      || scope.handoffId !== undefined || scope.agentActionId !== undefined) return fail()
    return value as VoiceInteractionHandoff
  }
  if (!handoffIdentity(value.handoff_id) || !identity(value.agent_action_id)
    || scope.handoffId !== undefined && value.handoff_id !== scope.handoffId
    || scope.agentActionId !== undefined && value.agent_action_id !== scope.agentActionId) return fail()
  // An invalidated pointer may no longer have a local progress target.
  if (value.deferred_progress !== null) validateDeferredVoiceOnboardingProgress(value.deferred_progress, scope.childId)
  else if (status === 'needs_deferral' || status === 'ready') return fail()
  if (status === 'ready') {
    const request = value.request
    if (!object(request) || Object.keys(request).some(key => !['transcript', 'client_action_id', 'context', 'voice_input_id'].includes(key))
      || typeof request.transcript !== 'string' || !request.transcript.trim() || !identity(request.client_action_id)
      || request.voice_input_id !== undefined && !identity(request.voice_input_id) || !object(request.context)
      || !['naming', 'confirm_name', 'tutorial'].includes(request.context.phase as string)
      || Object.keys(request.context).some(key => !['phase', 'step', 'pending_name', 'wake_word'].includes(key))
      || value.receipt !== null) return fail()
    const context = request.context as Record<string, unknown>
    if (['step', 'pending_name', 'wake_word'].some(key => context[key] !== undefined && typeof context[key] !== 'string')) return fail()
  } else if (value.request !== null) return fail()
  if (status === 'needs_deferral' && value.receipt !== null) return fail()
  if (value.receipt !== null) {
    const receipt = value.receipt
    const receiptStatus: Partial<Record<VoiceInteractionHandoffStatus, string>> = { received: 'received', interpreted: 'interpreted',
      completed: 'recorded_completed', failed: 'recorded_failed', outcome_unknown: 'unconfirmed' }
    if (!object(receipt) || !keys(receipt, ['session_id', 'client_action_id', 'status', 'response_text', 'local_only', 'cloud_transmission_allowed', 'replay_allowed', 'mastery_update_allowed'])
      || receipt.session_id !== scope.sessionId || receipt.client_action_id !== value.agent_action_id
      || !receiptStatus[status] || receipt.status !== receiptStatus[status]
      || typeof receipt.response_text !== 'string' || !receipt.response_text || receipt.response_text.length > 500
      || receipt.local_only !== true || receipt.cloud_transmission_allowed !== false
      || receipt.replay_allowed !== false || receipt.mastery_update_allowed !== false) return fail()
  }
  return value as VoiceInteractionHandoff
}
